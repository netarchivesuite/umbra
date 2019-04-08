#!/usr/bin/env python
# vim: set sw=4 et:

import json
import logging
import socket
import threading
import time
from typing import Set

from brozzler.browser import BrowsingException
from kombu import Consumer, Connection

import brozzler
import kombu
import urlcanon
from umbra.ReleasableBrowserPool import ReleasableBrowserPool

class AmqpBrowserController:
    """
    Consumes amqp messages representing requests to browse urls, from the
    specified amqp queue (default: "urls") on the specified amqp exchange
    (default: "umbra"). Incoming amqp message is a json object with 3
    attributes:

      {
        "clientId": "umbra.client.123",
        "url": "http://example.com/my_fancy_page",
        "behaviorParameters": {"some":"parameter","another":"thing"},
        "metadata": {"arbitrary":"fields", "etc":4}
      }

    "url" is the url to browse.

    "clientId" uniquely identifies the client of umbra. Umbra uses the clientId
    as the amqp routing key, to direct information via amqp back to the client.
    It sends this information on the same specified amqp exchange (default:
    "umbra").

    "behaviorParameters" are used to populate the javascript behavior template.

    Each url requested in the browser is published to amqp this way. The
    outgoing amqp message is a json object:

      {
        "url": "http://example.com/images/embedded_thing.jpg",
        "method": "GET",
        "headers": {"User-Agent": "...", "Accept": "...", ...},
        "parentUrl": "http://example.com/my_fancy_page",
        "parentUrlMetadata": {"arbitrary":"fields", "etc":4, ...}
      }

    POST requests have an additional field, postData.
    """
    _browsing_threads: Set[threading.Thread]

    logger = logging.getLogger(__module__ + "." + __qualname__)

    def __init__(self, amqp_url='amqp://guest:guest@localhost:5672/%2f',
            chrome_exe='chromium-browser', max_active_browsers=1,
            queue_name='urls', exchange_name='umbra', routing_key='urls'):
        self.amqp_url = amqp_url
        self.queue_name = queue_name
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.max_active_browsers = max_active_browsers

        self._browser_pool = ReleasableBrowserPool(
                size=max_active_browsers, chrome_exe=chrome_exe,
                ignore_cert_errors=True)

        self._browsing_threads = set()

    def start(self):
        self._browsing_threads.clear()
        self._browsing_threads_lock = threading.Lock()

        self._exchange = kombu.Exchange(name=self.exchange_name, type='direct',
                durable=True)

        self._reconnect_requested = False

        self._producer = None
        self._producer_lock = threading.Lock()
        with self._producer_lock:
            self._producer_conn = kombu.Connection(self.amqp_url)
            self._producer = self._producer_conn.Producer(serializer='json')

        self._consumer_thread = threading.Thread(target=self._consume_amqp, name='AmqpConsumerThread')
        self._consumer_stop = threading.Event()
        self._consumer_thread.start()

    def shutdown(self):
        self.logger.info("shutting down amqp consumer {}".format(self.amqp_url))
        self._consumer_stop.set()
        self._consumer_thread.join()

    def shutdown_now(self):
        self.logger.info("shutting down amqp consumer %s", self.amqp_url)
        self._consumer_stop.set()
        with self._browsing_threads_lock:
            for th in self._browsing_threads:
                if th.is_alive():
                    brozzler.thread_raise(th, brozzler.ShutdownRequested)
        # self._browser_pool.shutdown_now()
        self._consumer_thread.join()

    def reconnect(self, *args, **kwargs):
        self._reconnect_requested = True
        self._browser_pool.shutdown_now()

    def _wait_for_and_browse_urls(self, conn: Connection, consumer: Consumer, timeout):
        start = time.time()

        while not (self._consumer_stop.is_set() or time.time() - start >= timeout or self._reconnect_requested):

            self.logger.debug("Umbra browser pool have %d in use browsers and %d available",
                             self._browser_pool.num_in_use(), self._browser_pool.num_available())

            browser = None
            try:
                browser = self._browser_pool.acquire() # raises brozzler.browser.NoBrowsersAvailable if none available
            except brozzler.browser.NoBrowsersAvailable:
                # no browsers available
                self.logger.debug("No browser available so sleeping")
                time.sleep(0.5)
                continue
            except:
                self.logger.critical("problem with browser initialization", exc_info=True)
                time.sleep(0.5)
                continue
            self.logger.info("Aquired browser on port %s", browser.chrome.port)


            def callback(body, message):
                try:
                    client_id = body.get('clientId')
                    url = body['url']
                    metadata = body.get('metadata')
                    behavior_parameters = body.get('behaviorParameters')
                    username = body.get('username')
                    password = body.get('password')
                except:
                    self.logger.error("unable to decipher message %s",
                                      message, exc_info=True)
                    self.logger.error("discarding bad message")
                    message.reject()
                    return

                self.logger.info("Starting to browse url %s with browser %d", url, browser.chrome.port)

                self._start_browsing_page(
                    browser, message, client_id, url, metadata,
                    behavior_parameters, username, password)

            consumer.callbacks = [callback]

            try:
                while True:
                    try:
                        #Handle ONE event
                        conn.drain_events(timeout=0.5)
                        break # out of "while True", to take another go-around in the outer while loop
                    except socket.timeout:
                        pass
                    except socket.error:
                        self.logger.error("problem consuming messages from AMQP, will try reconnecting after active browsing finishes", exc_info=True)
                        self._reconnect_requested = True

                    if self._consumer_stop.is_set() or time.time() - start >= timeout or self._reconnect_requested:
                        self.logger.debug("Releasing browser on port %s", browser.chrome.port)
                        self._browser_pool.release(browser)
                        self.logger.debug("Released browser on port %s", browser.chrome.port)
                        break
            finally:
                self.logger.info("Consumer is done")
                consumer.callbacks = None

    def _wait_for_active_browsers(self, timeout=0):
        self.logger.info("waiting (for %d seconds) for browsing threads to finish", timeout)
        start = time.time()

        while True:
            with self._browsing_threads_lock:
                if len(self._browsing_threads) == 0:
                    break
            time.sleep(0.5)

            if  timeout > 0 and time.time() - start >= timeout:
                self.logger.info("Timeout %d reached, stopping browsers forcefully", timeout)
                break

        with self._browsing_threads_lock:
            self._browser_pool.release_everything()

        self.logger.info("active browsing threads finished")

    def _consume_amqp(self):
        # XXX https://webarchive.jira.com/browse/ARI-3811
        # After running for some amount of time (3 weeks in the latest case),
        # consumer looks normal but doesn't consume any messages. Not clear if
        # it's hanging in drain_events() or not. As a temporary measure for
        # mitigation (if it works) or debugging (if it doesn't work), close and
        # reopen the connection every 2.5 hours
        RECONNECT_AFTER_SECONDS = 150 * 60

        url_queue = kombu.Queue(self.queue_name, exchange=self._exchange, routing_key=self.routing_key)

        while not self._consumer_stop.is_set():
            try:
                self.logger.info("connecting to amqp exchange={} at {}".format(self._exchange.name, self.amqp_url))
                self._reconnect_requested = False
                with kombu.Connection(self.amqp_url) as conn:
                    conn.default_channel.basic_qos(
                            prefetch_count=self.max_active_browsers,
                            prefetch_size=0, a_global=False)
                    with conn.Consumer(url_queue) as consumer:
                        self._wait_for_and_browse_urls(
                                conn, consumer, timeout=RECONNECT_AFTER_SECONDS)

                    # need to wait for browsers to finish here, before closing
                    # the amqp connection,  because they use it to do
                    # message.ack() after they finish browsing a page
                    self._wait_for_active_browsers(timeout=RECONNECT_AFTER_SECONDS)
            except BaseException as e:
                self.logger.error("caught exception {}".format(e), exc_info=True)
                time.sleep(0.5)
                self.logger.error("attempting to reopen amqp connection")

    def _start_browsing_page(
            self, browser, message, client_id, url, parent_url_metadata,
            behavior_parameters=None, username=None, password=None):
        def on_response(chrome_msg):
            if (chrome_msg['params']['response']['url'].lower().startswith('data:')
                    or chrome_msg['params']['response']['fromDiskCache']
                    or not 'requestHeaders' in chrome_msg['params']['response']):
                return

            payload = {
                'url': chrome_msg['params']['response']['url'],
                'headers': chrome_msg['params']['response']['requestHeaders'],
                'parentUrl': url,
                'parentUrlMetadata': parent_url_metadata,
            }

            if ':method' in chrome_msg['params']['response']['requestHeaders']:
                # happens when http transaction is http 2.0
                payload['method'] = chrome_msg['params']['response']['requestHeaders'][':method']
            elif 'requestHeadersText' in chrome_msg['params']['response']:
                req = chrome_msg['params']['response']['requestHeadersText']
                payload['method'] = req[:req.index(' ')]
            else:
                self.logger.warning('unable to identify http method (assuming GET) chrome_msg=%s',
                                 chrome_msg)
                payload['method'] = 'GET'

            self.logger.debug(
                    'sending to amqp exchange=%s routing_key=%s payload=%s',
                    self.exchange_name, client_id, payload)
            with self._producer_lock:
                publish = self._producer_conn.ensure(self._producer,
                                                     self._producer.publish)
                publish(payload, exchange=self._exchange, routing_key=client_id)

        def post_outlinks(outlinks=None):
            def prune_outlinks(dirty_links, block_list=None):
                '''
                Filter for valid schemes, remove URL fragments, and drop any other designated URLs from the list.
                '''
                links = set()
                dirty_links = set(dirty_links)

                self.logger.info('Pruning links...')
                for link in dirty_links:
                    link = urlcanon.parse_url(link)

                    if link.scheme in (b'http', b'https', b'ftp'):
                        urlcanon.canon.remove_fragment(link)
                        link = str(link).strip()
                        links.add(link)

                self.logger.info('Pruning complete.')

                # Need to remove after link fragments have been removed to prevent duplication.
                self.logger.info('Removing Links: %s', ', '.join(block_list))
                links = links.difference(block_list)

                return links

            outlinks = prune_outlinks(outlinks, {url})

            self.logger.info('Posting Outlinks:\n\t%s', '\n\t'.join(sorted(outlinks)))

            for link in outlinks:
                 #  Each of these payload fields are required by AMQPUrlReceiver.java
                 #+ in Heritrix.
                 payload = {
                    'url': link,
                    'headers': {},
                    'parentUrl': url,
                    'parentUrlMetadata': parent_url_metadata,
                    'method': 'GET',
                 }
                 self.logger.debug(
                            'sending outlink to amqp exchange=%s routing_key=%s payload=%s',
                            self.exchange_name, client_id, payload)
                 with self._producer_lock:
                     publish = self._producer_conn.ensure(self._producer,
                                                          self._producer.publish)
                     publish(payload, exchange=self._exchange, routing_key=client_id)

        def browse_page_sync():
            self.logger.info(
                    'browser=%s client_id=%s behavior_parameters=%s',
                    browser, client_id, behavior_parameters)
            try:
                final_page_url, outlinks = browser.browse_page(
                        url, on_response=on_response,
                        behavior_parameters=behavior_parameters,
                        skip_visit_hashtags=True,
                        username=username, password=password)

                # Temporarily commenting out for https://webarchive.jira.com/browse/AITFIVE-1295
                #post_outlinks(outlinks)

                message.ack()
                self.logger.info("Completed outlink extraction")
            # except brozzler.PageInterstitialShown as e:
            #     self.logger.info("page interstitial shown, likely unsupported http auth, for url {} - {}".format(url, e))
            #     message.reject()
            except brozzler.ShutdownRequested as e:
                self.logger.info("browsing did not complete normally, requeuing - {}".format(e))
                message.requeue()  # republish?
            except BrowsingException as e:
                self.logger.warning("browsing did not complete normally, republishing - {}".format(e))
                republish_amqp(self, message)
            except:
                self.logger.critical("problem browsing page, republishing, may have lost browser process {}".format(browser), exc_info=True)
                republish_amqp(self, message)

        def republish_amqp(self, message):
            # republish on exception, not requeue!
            payload = json.loads(to_str(message.body))
            message.ack()
            max_retries = 5
            if 'metadata' in payload:
                if not 'retries' in payload['metadata']:
                    payload['metadata']['retries'] = 1
                else:
                    if payload['metadata']['retries'] >= max_retries:
                        return
                    payload['metadata']['retries'] += 1
            self.logger.debug(
                       're-publishing url to amqp exchange=%s routing_key=%s payload=%s',
                       self.exchange_name, self.routing_key, payload)
            with self._producer_lock:
                publish = self._producer_conn.ensure(self._producer,
                                                     self._producer.publish)
                publish(payload, exchange=self._exchange, routing_key=self.routing_key)

        def to_str(bytes_or_str):
            if isinstance(bytes_or_str, bytes):
                value = bytes_or_str.decode() # uses 'utf-8' for encoding
            else:
                value = bytes_or_str
            return value # Instance of str

        def browse_thread_run_then_cleanup():
            th = threading.current_thread()
            try: # Try to remove browsing thread

                try: #Try for browser pool release

                    try: #Try for browser start with finally stop
                        self.logger.info("Starting browser")
                        browser.start()

                        self.logger.info("Starting page sync")
                        browse_page_sync()
                        self.logger.info("Completed page sync")
                    finally:
                        self.logger.debug("Stopping browser")
                        #Stop the started browser, no matter what happens
                        browser.stop()
                        self.logger.debug("Stopped browser")
                finally:
                    self.logger.debug("Releasing browser")
                    self._browser_pool.release(browser)
                    self.logger.debug("Released browser")
            finally:
                #And as this is the last of this thread, deregister it
                self.logger.debug(
                    'Removing from self._browsing_threads')
                with self._browsing_threads_lock:
                    self._browsing_threads.remove(th)


        thread_name = "BrowsingThread:%s-%s" % (browser.chrome.port,url)
        th = threading.Thread(target=browse_thread_run_then_cleanup, name=thread_name)

        self.logger.info('adding thread %s to self._browsing_threads', th)
        with self._browsing_threads_lock:
            self._browsing_threads.add(th)

        th.start()

