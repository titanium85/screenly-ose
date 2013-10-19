#!/usr/bin/env python
# -*- coding: utf8 -*-

__author__ = "Viktor Petersson"
__copyright__ = "Copyright 2012-2013, WireLoad Inc"
__license__ = "Dual License: GPLv2 and Commercial License"

from datetime import datetime, timedelta
from glob import glob
from os import path, getenv, remove, makedirs
from os import stat as os_stat, utime, system, kill
from platform import machine
from random import shuffle
from requests import get as req_get, head as req_head
from time import sleep, time
import json
import logging
import sh
import signal

from settings import settings
import html_templates

from utils import url_fails

import db
import assets_helper
# Define to none to ensure we refresh
# the settings.
last_settings_refresh = None
current_browser_url = None

BLACK_PAGE = '/tmp/screenly_html/black_page.html'
SCREENLY_HTML = '/tmp/screenly_html/'



def sigusr1(signum, frame):
    """
    The signal interrupts sleep() calls, so the currently playing web or image asset is skipped.
    omxplayer is killed to skip any currently playing video assets.
    """
    logging.info('USR1 received, skipping.')
    sh.killall('omxplayer.bin')


def sigusr2(signum, frame):
    """Reload settings"""
    global last_settings_refresh
    logging.info("USR2 received, reloading settings.")
    last_settings_refresh = None
    reload_settings()


class Scheduler(object):
    def __init__(self, *args, **kwargs):
        logging.debug('Scheduler init')
        self.update_playlist()

    def get_next_asset(self):
        logging.debug('get_next_asset')
        self.refresh_playlist()
        logging.debug('get_next_asset after refresh')
        if self.nassets == 0:
            return None
        idx = self.index
        self.index = (self.index + 1) % self.nassets
        logging.debug('get_next_asset counter %s returning asset %s of %s', self.counter, idx + 1, self.nassets)
        if settings['shuffle_playlist'] and self.index == 0:
            self.counter += 1
        return self.assets[idx]

    def refresh_playlist(self):
        logging.debug('refresh_playlist')
        time_cur = datetime.utcnow()
        logging.debug('refresh: counter: (%s) deadline (%s) timecur (%s)', self.counter, self.deadline, time_cur)
        if self.dbisnewer():
            self.update_playlist()
        elif settings['shuffle_playlist'] and self.counter >= 5:
            self.update_playlist()
        elif self.deadline and self.deadline <= time_cur:
            self.update_playlist()

    def update_playlist(self):
        logging.debug('update_playlist')
        (self.assets, self.deadline) = generate_asset_list()
        self.nassets = len(self.assets)
        self.gentime = time()
        self.counter = 0
        self.index = 0
        logging.debug('update_playlist done, count %s, counter %s, index %s, deadline %s', self.nassets, self.counter, self.index, self.deadline)

    def dbisnewer(self):
        # get database file last modification time
        try:
            db_mtime = path.getmtime(settings['database'])
        except:
            db_mtime = 0
        return db_mtime >= self.gentime


def generate_asset_list():
    logging.info('Generating asset-list...')
    playlist = assets_helper.get_playlist(db_conn)
    deadline = sorted([asset['end_date'] for asset in playlist])[0] if len(playlist) > 0 else None
    logging.debug('generate_asset_list deadline: %s', deadline)

    if settings['shuffle_playlist']:
        shuffle(playlist)

    return (playlist, deadline)


def watchdog():
    """
    Notify the watchdog file to be used with the watchdog-device.
    """

    watchdog = '/tmp/screenly.watchdog'
    if not path.isfile(watchdog):
        open(watchdog, 'w').close()
    else:
        utime(watchdog, None)



def load_browser(url=None):
    global browser, current_browser_url
    logging.info('Loading browser...')

    if browser:
        logging.info('killing previous uzbl %s', browser.pid)
        browser.process.kill()

    if not url is None:
        current_browser_url = url

    # --config=-       read commands (and config) from stdin
    # --print-events   print events to stdout
    # ---uri=URI       URI to load on start
    browser = sh.Command('uzbl-browser')(print_events=True, config='-', uri=current_browser_url, _bg=True)
    logging.info('Browser loading %s. Running as PID %s.', current_browser_url, browser.pid)

    uzbl_rc = 'ssl_verify {}\n'.format('1' if settings['verify_ssl'] else '0')
    with open(HOME + UZBLRC) as f:  # load uzbl.rc
        uzbl_rc = f.read() + uzbl_rc
    browser_send(uzbl_rc)


def browser_send(command, cb=lambda _: True):
    if not (browser is None) and browser.process.alive:
        while not browser.process._pipe_queue.empty():  # flush stdout
            browser.next()

        browser.process.stdin.put(command + '\n')
        while True:  # loop until cb returns True
            if cb(browser.next()):
                break
    else:
        logging.info('browser found dead, restarting')
        load_browser()


def browser_clear(force=False):
    """Load a black page. Default cb waits for the page to load."""
    browser_url(BLACK_PAGE, force=force, cb=lambda buf: 'LOAD_FINISH' in buf and BLACK_PAGE in buf)


def browser_url(url, cb=lambda _: True, force=False):
    global current_browser_url

    if url == current_browser_url and not force:
        logging.debug('Already showing %s, keeping it.', current_browser_url)
    else:
        current_browser_url = url
        browser_send('uri ' + current_browser_url, cb=cb)
        logging.info('current url is %s', current_browser_url)


def view_image(uri):
    browser_clear()
    browser_send('js window.setimg("{0}")'.format(uri), cb=lambda b: 'COMMAND_EXECUTED' in b and 'setimg' in b)


def view_video(uri):
    logging.debug('Displaying video %s', uri)

    if arch == 'armv6l':
        run = sh.omxplayer(uri, o=settings['audio_output'], _bg=True)
    else:
        run = sh.mplayer(uri, '-nosound', _bg=True)

    browser_clear(force=True)
    run.wait()


def check_update():
    """
    Check if there is a later version of Screenly-OSE
    available. Only do this update once per day.

    Return True if up to date was written to disk,
    False if no update needed and None if unable to check.
    """

    sha_file = path.join(settings.get_configdir(), 'latest_screenly_sha')

    if path.isfile(sha_file):
        sha_file_mtime = path.getmtime(sha_file)
        last_update = datetime.fromtimestamp(sha_file_mtime)
    else:
        last_update = None

    logging.debug('Last update: %s' % str(last_update))

    if last_update is None or last_update < (datetime.now() - timedelta(days=1)):

        if not url_fails('http://stats.screenlyapp.com'):
            latest_sha = req_get('http://stats.screenlyapp.com/latest')

            if latest_sha.status_code == 200:
                with open(sha_file, 'w') as f:
                    f.write(latest_sha.content.strip())
                return True
            else:
                logging.debug('Received on 200-status')
                return
        else:
            logging.debug('Unable to retreive latest SHA')
            return
    else:
        return False


def reload_settings():
    """
    Reload settings if the timestamp of the
    settings file is newer than the settings
    file loaded in memory.
    """

    settings_file = settings.get_configfile()
    settings_file_mtime = path.getmtime(settings_file)
    settings_file_timestamp = datetime.fromtimestamp(settings_file_mtime)

    if not last_settings_refresh or settings_file_timestamp > last_settings_refresh:
        settings.load()

    logging.getLogger().setLevel(logging.DEBUG if settings['debug_logging'] else logging.INFO)

    global last_setting_refresh
    last_setting_refresh = datetime.utcnow()


def pro_init():
    """Function to handle first-run on Screenly Pro"""
    is_pro_init = path.isfile(path.join(settings.get_configdir(), 'not_initialized'))
    intro_file = path.join(settings.get_configdir(), 'intro.html')

    if is_pro_init:
        logging.debug('Detected Pro initiation cycle.')
        while not path.isfile(intro_file):
            logging.debug('intro.html missing. Going to sleep.')
            sleep(5)
        load_browser(url=intro_file)
    else:
        return False

    status_path = path.join(settings.get_configdir(), 'setup_status.json')
    while is_pro_init:
        with open(status_path, 'rb') as status_file:
            status = json.load(status_file)

        browser_send('js showUpdating()' if status['claimed'] else
                     'js showPin("{0}")'.format(status['pin']))

        logging.debug('Waiting for node to be initialized.')
        sleep(5)

    return True


def asset_loop():
    scheduler = Scheduler()
    logging.debug('Entering infinite loop.')
    while True:
        check_update()
        asset = scheduler.get_next_asset()

        if asset is None:
            logging.info('Playlist is empty. Sleeping for %s seconds', EMPTY_PL_DELAY)
            view_image(HOME + LOAD_SCREEN)
            sleep(EMPTY_PL_DELAY)

        elif path.isfile(asset['uri']) or not url_fails(asset['uri']):
            name, mime, uri = asset['name'], asset['mimetype'], asset['uri']
            logging.info('Showing asset %s (%s)', name, mime)
            logging.debug('Asset URI %s', uri)
            watchdog()

            if 'image' in mime:
                view_image(uri)
            elif 'web' in mime:
                browser_url(uri)
            elif 'video' in mime:
                view_video(uri)
            else:
                logging.error('Unknown MimeType %s', mime)

            if 'image' in mime or 'web' in mime:
                duration = int(asset['duration'])
                logging.info('Sleeping for %s', duration)
                sleep(duration)
        else:
            logging.info('Asset %s at %s is not available, skipping.', asset['name'], asset['uri'])
            sleep(0.5)


def setup():
    global HOME, arch, db_conn
    HOME = getenv('HOME', '/home/pi')
    arch = machine()

    signal.signal(signal.SIGUSR1, sigusr1)
    signal.signal(signal.SIGUSR2, sigusr2)

    reload_settings()
    db_conn = db.conn(settings['database'])

    if not path.isdir(SCREENLY_HTML):
        makedirs(SCREENLY_HTML)

    html_templates.black_page(BLACK_PAGE)


def main():
    setup()
    if pro_init():
        return

    url = 'http://{0}:{1}/splash_page'.format(settings.get_listen_ip(), settings.get_listen_port()) if settings['show_splash'] else BLACK_PAGE
    load_browser(url=url)

    if settings['show_splash']:
        sleep(60)

    asset_loop()


if __name__ == "__main__":
    main()
