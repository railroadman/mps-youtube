"""
mps-youtube.

https://github.com/np1/mps-youtube

Copyright (C) 2014, 2015 np1 and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""

from xml.etree import ElementTree as ET
import subprocess
import traceback
import difflib
import logging
import base64
import random
import locale
import socket
import shlex
import time
import math
import json
import sys
import re
import os
import webbrowser
from urllib.request import urlopen, build_opener
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

import pafy
from pafy import call_gdata, GdataError

from . import g, c, commands, cache, streams, screen, content, history
from . import __version__, __url__, playlists
from .content import generate_songlist_display, generate_playlist_display
from .content import logo, playlists_display
from .playlist import Playlist, Video
from .config import Config, known_player_set
from .util import dbg, get_near_name, yt_datetime
from .util import get_pafy, getxy, fmt_time, parse_multi
from .util import xenc, xprint, mswinfn, set_window_title, F
from .helptext import get_help
from .player import play_range

try:
    import readline
    readline.set_history_length(2000)
    has_readline = True

except ImportError:
    has_readline = False

try:
    # pylint: disable=F0401
    import pyperclip
    has_pyperclip = True

except ImportError:
    has_pyperclip = False


mswin = os.name == "nt"

locale.setlocale(locale.LC_ALL, "")  # for date formatting

ISO8601_TIMEDUR_EX = re.compile(r'PT((\d{1,3})H)?((\d{1,3})M)?((\d{1,2})S)?')


class IterSlicer():
    """ Class that takes an iterable and allows slicing,
        loading from the iterable as needed."""

    def __init__(self, iterable, length=None):
        self.ilist = []
        self.iterable = iter(iterable)
        self.length = length
        if length is None:
            try:
                self.length = len(iterable)
            except TypeError:
                pass

    def __getitem__(self, sliced):
        if isinstance(sliced, slice):
            stop = sliced.stop
        else:
            stop = sliced
        # To get the last item in an iterable, must iterate over all items
        if (stop is None) or (stop < 0):
            stop = None
        while (stop is None) or (stop > len(self.ilist) - 1):
            try:
                self.ilist.append(next(self.iterable))
            except StopIteration:
                break

        return self.ilist[sliced]

    def __len__(self):
        if self.length is None:
            self.length = len(self[:])
        return self.length


@commands.command(r'set|showconfig')
def showconfig():
    """ Dump config data. """
    width = getxy().width
    width -= 30
    s = "  %s%-17s%s : %s\n"
    out = "  %s%-17s   %s%s%s\n" % (c.ul, "Key", "Value", " " * width, c.w)

    for setting in Config:
        val = Config[setting]

        # don't show player specific settings if unknown player
        if not known_player_set() and val.require_known_player:
            continue

        # don't show max_results if auto determined
        if g.detectable_size and setting == "MAX_RESULTS":
            continue

        if g.detectable_size and setting == "CONSOLE_WIDTH":
            continue

        out += s % (c.g, setting.lower(), c.w, val.display)

    g.content = out
    g.message = "Enter %sset <key> <value>%s to change\n" % (c.g, c.w)
    g.message += "Enter %sset all default%s to reset all" % (c.g, c.w)


@commands.command(r'set\s+([-\w]+)\s*(.*)')
def setconfig(key, val):
    """ Set configuration variable. """
    key = key.replace("-", "_")
    if key.upper() == "ALL" and val.upper() == "DEFAULT":

        for ci in Config:
            Config[ci].value = Config[ci].default

        Config.save()
        message = "Default configuration reinstated"

    elif not key.upper() in Config:
        message = "Unknown config item: %s%s%s" % (c.r, key, c.w)

    elif val.upper() == "DEFAULT":
        att = Config[key.upper()]
        att.value = att.default
        message = "%s%s%s set to %s%s%s (default)"
        dispval = att.display or "None"
        message = message % (c.y, key, c.w, c.y, dispval, c.w)
        Config.save()

    else:
        # Config.save() will be called by Config.set() method
        message = Config[key.upper()].set(val)

    showconfig()
    g.message = message


def get_track_id_from_json(item):
    """ Try to extract video Id from various response types """
    fields = ['contentDetails/videoId',
              'snippet/resourceId/videoId',
              'id/videoId',
              'id']
    for field in fields:
        node = item
        for p in field.split('/'):
            if node and type(node) is dict:
                node = node.get(p)
        if node:
            return node
    return ''


def get_tracks_from_json(jsons):
    """ Get search results from API response """

    items = jsons.get("items")
    if not items:
        dbg("got unexpected data or no search results")
        return ()

    # fetch detailed information about items from videos API
    qs = {'part':'contentDetails,statistics,snippet',
          'id': ','.join([get_track_id_from_json(i) for i in items])}

    wdata = call_gdata('videos', qs)

    items_vidinfo = wdata.get('items', [])
    # enhance search results by adding information from videos API response
    for searchresult, vidinfoitem in zip(items, items_vidinfo):
        searchresult.update(vidinfoitem)

    # populate list of video objects
    songs = []
    for item in items:

        try:

            ytid = get_track_id_from_json(item)
            duration = item.get('contentDetails', {}).get('duration')

            if duration:
                duration = ISO8601_TIMEDUR_EX.findall(duration)
                if len(duration) > 0:
                    _, hours, _, minutes, _, seconds = duration[0]
                    duration = [seconds, minutes, hours]
                    duration = [int(v) if len(v) > 0 else 0 for v in duration]
                    duration = sum([60**p*v for p, v in enumerate(duration)])
                else:
                    duration = 30
            else:
                duration = 30

            stats = item.get('statistics', {})
            snippet = item.get('snippet', {})
            title = snippet.get('title', '').strip()
            # instantiate video representation in local model
            cursong = Video(ytid=ytid, title=title, length=duration)
            likes = int(stats.get('likeCount', 0))
            dislikes = int(stats.get('dislikeCount', 0))
            #XXX this is a very poor attempt to calculate a rating value
            rating = 5.*likes/(likes+dislikes) if (likes+dislikes) > 0 else 0
            category = snippet.get('categoryId')

            # cache video information in custom global variable store
            g.meta[ytid] = dict(
                # tries to get localized title first, fallback to normal title
                title=snippet.get('localized',
                                  {'title':snippet.get('title',
                                                       '[!!!]')}).get('title',
                                                                      '[!]'),
                length=str(fmt_time(cursong.length)),
                rating=str('{}'.format(rating))[:4].ljust(4, "0"),
                uploader=snippet.get('channelId'),
                uploaderName=snippet.get('channelTitle'),
                category=category,
                aspect="custom", #XXX
                uploaded=yt_datetime(snippet.get('publishedAt', ''))[1],
                likes=str(num_repr(likes)),
                dislikes=str(num_repr(dislikes)),
                commentCount=str(num_repr(int(stats.get('commentCount', 0)))),
                viewCount=str(num_repr(int(stats.get('viewCount', 0)))))

        except Exception as e:

            dbg(json.dumps(item, indent=2))
            dbg('Error during metadata extraction/instantiation of search ' +
                'result {}\n{}'.format(ytid, e))

        songs.append(cursong)

    # return video objects
    return songs


def num_repr(num):
    """ Return up to four digit string representation of a number, eg 2.6m. """
    if num <= 9999:
        return str(num)

    def digit_count(x):
        """ Return number of digits. """
        return int(math.floor(math.log10(x)) + 1)

    digits = digit_count(num)
    sig = 3 if digits % 3 == 0 else 2
    rounded = int(round(num, int(sig - digits)))
    digits = digit_count(rounded)
    suffix = "_kmBTqXYX"[(digits - 1) // 3]
    front = 3 if digits % 3 == 0 else digits % 3

    if not front == 1:
        return str(rounded)[0:front] + suffix

    return str(rounded)[0] + "." + str(rounded)[1] + suffix


def _search(progtext, qs=None, msg=None, failmsg=None):
    """ Perform memoized url fetch, display progtext. """
    
    loadmsg = "Searching for '%s%s%s'" % (c.y, progtext, c.w)

    wdata = call_gdata('search', qs)

    def iter_songs():
        wdata2 = wdata
        while True:
            for song in get_tracks_from_json(wdata2):
                yield song

            if not wdata2.get('nextPageToken'):
                break
            qs['pageToken'] = wdata2['nextPageToken']
            wdata2 = call_gdata('search', qs)

    # The youtube search api returns a maximum of 500 results
    length = min(wdata['pageInfo']['totalResults'], 500)
    slicer = IterSlicer(iter_songs(), length)

    paginatesongs(slicer, length=length, msg=msg, failmsg=failmsg,
            loadmsg=loadmsg)


def token(page):
    """ Returns a page token for a given start index. """
    index = (page or 0) * getxy().max_results
    k = index//128 - 1
    index -= 128 * k
    f = [8, index]
    if k > 0 or index > 127:
        f.append(k+1)
    f += [16, 0]
    b64 = base64.b64encode(bytes(f)).decode('utf8')
    return b64.strip('=')


def generate_search_qs(term, match='term'):
    """ Return query string. """

    aliases = dict(views='viewCount')
    qs = {
        'q': term,
        'maxResults': 50,
        'safeSearch': "none",
        'order': aliases.get(Config.ORDER.get, Config.ORDER.get),
        'part': 'id,snippet',
        'type': 'video',
        'key': Config.API_KEY.get
    }

    if match == 'related':
        qs['relatedToVideoId'] = term
        del qs['q']

    if Config.SEARCH_MUSIC.get:
        qs['videoCategoryId'] = 10

    return qs


def userdata_cached(userterm):
    """ Check if user name search term found in cache """
    userterm = ''.join([t.strip().lower() for t in userterm.split(' ')])
    return g.username_query_cache.get(userterm)


def cache_userdata(userterm, username, channel_id):
    """ Cache user name and channel id tuple """
    userterm = ''.join([t.strip().lower() for t in userterm.split(' ')])
    g.username_query_cache[userterm] = (username, channel_id)
    dbg('Cache data for username search query "{}": {} ({})'.format(
        userterm, username, channel_id))

    while len(g.username_query_cache) > 300:
        g.username_query_cache.popitem(last=False)
    return (username, channel_id)


def channelfromname(user):
    """ Query channel id from username. """

    cached = userdata_cached(user)
    if cached:
        user, channel_id = cached
    else:
        # if the user is looked for by their display name,
        # we have to sent an additional request to find their
        # channel id
        qs = {'part': 'id,snippet',
              'maxResults': 1,
              'q': user,
              'type': 'channel'}

        try:
            userinfo = call_gdata('search', qs)['items']
            if len(userinfo) > 0:
                snippet = userinfo[0].get('snippet', {})
                channel_id = snippet.get('channelId', user)
                username = snippet.get('title', user)
                user = cache_userdata(user, username, channel_id)[0]
            else:
                g.message = "User {} not found.".format(c.y + user + c.w)
                return

        except GdataError as e:
            g.message = "Could not retrieve information for user {}\n{}".format(
                c.y + user + c.w, e)
            dbg('Error during channel request for user {}:\n{}'.format(
                user, e))
            return

    # at this point, we know the channel id associated to a user name
    return (user, channel_id)


@commands.command(r'user\s+(.+)')
def usersearch(q_user, identify='forUsername'):
    """ Fetch uploads by a YouTube user. """

    user, _, term = (x.strip() for x in q_user.partition("/"))
    if identify == 'forUsername':
        ret = channelfromname(user)
        if not ret: # Error
            return
        user, channel_id = ret

    else:
        channel_id = user

    # at this point, we know the channel id associated to a user name
    usersearch_id(user, channel_id, term)


def usersearch_id(user, channel_id, term):
    """ Performs a search within a user's (i.e. a channel's) uploads
    for an optional search term with the user (i.e. the channel)
    identified by its ID """

    query = generate_search_qs(term)
    aliases = dict(views='viewCount')  # The value of the config item is 'views' not 'viewCount'
    if Config.USER_ORDER.get:
        query['order'] = aliases.get(Config.USER_ORDER.get,
                Config.USER_ORDER.get)
    query['channelId'] = channel_id

    termuser = tuple([c.y + x + c.w for x in (term, user)])
    if term:
        msg = "Results for {1}{3}{0} (by {2}{4}{0})"
        progtext = "%s by %s" % termuser
        failmsg = "No matching results for %s (by %s)" % termuser
    else:
        msg = "Video uploads by {2}{4}{0}"
        progtext = termuser[1]
        if Config.SEARCH_MUSIC:
            failmsg = """User %s not found or has no videos in the Music category.
Use 'set search_music False' to show results not in the Music category.""" % termuser[1]
        else:
            failmsg = "User %s not found or has no videos."  % termuser[1]
    msg = str(msg).format(c.w, c.y, c.y, term, user)

    _search(progtext, query, msg, failmsg)


def related_search(vitem):
    """ Fetch uploads by a YouTube user. """
    query = generate_search_qs(vitem.ytid, match='related')

    if query.get('videoCategoryId'):
        del query['videoCategoryId']

    t = vitem.title
    ttitle = t[:48].strip() + ".." if len(t) > 49 else t

    msg = "Videos related to %s%s%s" % (c.y, ttitle, c.w)
    failmsg = "Related to %s%s%s not found" % (c.y, vitem.ytid, c.w)
    _search(ttitle, query, msg, failmsg)


# Note: [^./] is to prevent overlap with playlist search command
@commands.command(r'(?:search|\.|/)\s*([^./].{1,500})')
def search(term):
    """ Perform search. """
    if not term or len(term) < 2:
        g.message = c.r + "Not enough input" + c.w
        g.content = generate_songlist_display()
        return

    logging.info("search for %s", term)
    query = generate_search_qs(term)
    msg = "Search results for %s%s%s" % (c.y, term, c.w)
    failmsg = "Found nothing for %s%s%s" % (c.y, term, c.w)
    _search(term, query, msg, failmsg)


@commands.command(r'u(?:ser)?pl\s(.*)')
def user_pls(user):
    """ Retrieve user playlists. """
    return pl_search(user, is_user=True)


@commands.command(r'(?:\.\.|\/\/|pls(?:earch)?\s)\s*(.*)')
def pl_search(term, page=0, splash=True, is_user=False):
    """ Search for YouTube playlists.

    term can be query str or dict indicating user playlist search.

    """
    if not term or len(term) < 2:
        g.message = c.r + "Not enough input" + c.w
        g.content = generate_songlist_display()
        return

    if splash:
        g.content = logo(c.g)
        prog = "user: " + term if is_user else term
        g.message = "Searching playlists for %s" % c.y + prog + c.w
        screen.update()

    if is_user:
        ret = channelfromname(term)
        if not ret: # Error
            return
        user, channel_id = ret

    else:
        # playlist search is done with the above url and param type=playlist
        logging.info("playlist search for %s", prog)
        qs = generate_search_qs(term)
        qs['pageToken'] = token(page)
        qs['type'] = 'playlist'
        if 'videoCategoryId' in qs:
            del qs['videoCategoryId'] # Incompatable with type=playlist

        pldata = call_gdata('search', qs)
        id_list = [i.get('id', {}).get('playlistId')
                    for i in pldata.get('items', ())]

        result_count = min(pldata['pageInfo']['totalResults'], 500)

    qs = {'part': 'contentDetails,snippet',
          'maxResults': 50}

    if is_user:
        if page:
            qs['pageToken'] = token(page)
        qs['channelId'] = channel_id
    else:
        qs['id'] = ','.join(id_list)

    pldata = call_gdata('playlists', qs)
    playlists = get_pl_from_json(pldata)[:getxy().max_results]

    if is_user:
        result_count = pldata['pageInfo']['totalResults']

    if playlists:
        g.last_search_query = (pl_search, {"term": term, "is_user": is_user})
        g.browse_mode = "ytpl"
        g.current_page = page
        g.result_count = result_count
        g.ytpls = playlists
        g.message = "Playlist results for %s" % c.y + prog + c.w
        g.content = generate_playlist_display()

    else:
        g.message = "No playlists found for: %s" % c.y + prog + c.w
        g.current_page = 0
        g.content = generate_songlist_display(zeromsg=g.message)


def get_pl_from_json(pldata):
    """ Process json playlist data. """

    try:
        items = pldata['items']

    except KeyError:
        items = []

    results = []

    for item in items:
        snippet = item['snippet']
        results.append(dict(
            link=item["id"],
            size=item["contentDetails"]["itemCount"],
            title=snippet["title"],
            author=snippet["channelTitle"],
            created=snippet["publishedAt"],
            updated=snippet['publishedAt'], #XXX Not available in API?
            description=snippet["description"]))

    return results


def fetch_comments(item):
    """ Fetch comments for item using gdata. """
    # pylint: disable=R0912
    # pylint: disable=R0914
    ytid, title = item.ytid, item.title
    dbg("Fetching comments for %s", c.c("y", ytid))
    screen.writestatus("Fetching comments for %s" % c.c("y", title[:55]))
    qs = {'textFormat': 'plainText',
          'videoId': ytid,
          'maxResults': 50,
          'part': 'snippet'}

    # XXX should comment threads be expanded? this would require
    # additional requests for comments responding on top level comments

    jsdata = call_gdata('commentThreads', qs)

    coms = jsdata.get('items', [])
    coms = [x.get('snippet', {}) for x in coms]
    coms = [x.get('topLevelComment', {}) for x in coms]
    # skip blanks
    coms = [x for x in coms if len(x.get('snippet', {}).get('textDisplay', '').strip())]
    if not len(coms):
        g.message = "No comments for %s" % item.title[:50]
        g.content = generate_songlist_display()
        return

    commentstext = ''

    for n, com in enumerate(coms, 1):
        snippet = com.get('snippet', {})
        poster = snippet.get('authorDisplayName')
        _, shortdate = yt_datetime(snippet.get('publishedAt', ''))
        text = snippet.get('textDisplay', '')
        cid = ("%s/%s" % (n, len(coms)))
        commentstext += ("%s %-35s %s\n" % (cid, c.c("g", poster), shortdate))
        commentstext += c.c("y", text.strip()) + '\n\n'

    g.content = content.StringContent(commentstext)


@commands.command(r'c\s?(\d{1,4})')
def comments(number):
    """ Receive use request to view comments. """
    if g.browse_mode == "normal":
        item = g.model[int(number) - 1]
        fetch_comments(item)

    else:
        g.content = generate_songlist_display()
        g.message = "Comments only available for video items"


def _make_fname(song, ext=None, av=None, subdir=None):
    """" Create download directory, generate filename. """
    # pylint: disable=E1103
    # Instance of 'bool' has no 'extension' member (some types not inferable)
    ddir = os.path.join(Config.DDIR.get, subdir) if subdir else Config.DDIR.get
    if not os.path.exists(ddir):
        os.makedirs(ddir)

    if not ext:
        stream = streams.select(streams.get(song),
                audio=av == "audio", m4a_ok=True)
        ext = stream['ext']

    # filename = song.title[:59] + "." + ext
    filename = song.title + "." + ext
    filename = os.path.join(ddir, mswinfn(filename.replace("/", "-")))
    filename = filename.replace('"', '')
    return filename


def extract_metadata(name):
    """ Try to determine metadata from video title. """
    seps = name.count(" - ")
    artist = title = None

    if seps == 1:

        pos = name.find(" - ")
        artist = name[:pos].strip()
        title = name[pos + 3:].strip()

    else:
        title = name.strip()

    return dict(artist=artist, title=title)


def remux_audio(filename, title):
    """ Remux audio file. Insert limited metadata tags. """
    dbg("starting remux")
    temp_file = filename + "." + str(random.randint(10000, 99999))
    os.rename(filename, temp_file)
    meta = extract_metadata(title)
    metadata = ["title=%s" % meta["title"]]

    if meta["artist"]:
        metadata = ["title=%s" % meta["title"], "-metadata",
                    "artist=%s" % meta["artist"]]

    cmd = [g.muxapp, "-y", "-i", temp_file, "-acodec", "copy", "-metadata"]
    cmd += metadata + ["-vn", filename]
    dbg(cmd)

    try:
        with open(os.devnull, "w") as devnull:
            subprocess.call(cmd, stdout=devnull, stderr=subprocess.STDOUT)

    except OSError:
        dbg("Failed to remux audio using %s", g.muxapp)
        os.rename(temp_file, filename)

    else:
        os.unlink(temp_file)
        dbg("remuxed audio file using %s" % g.muxapp)


def transcode(filename, enc_data):
    """ Re encode a download. """
    base = os.path.splitext(filename)[0]
    exe = g.muxapp if g.transcoder_path == "auto" else g.transcoder_path

    # ensure valid executable
    if not exe or not os.path.exists(exe) or not os.access(exe, os.X_OK):
        xprint("Encoding failed. Couldn't find a valid encoder :(\n")
        time.sleep(2)
        return filename

    command = shlex.split(enc_data['command'])
    newcom, outfn = command[::], ""

    for n, d in enumerate(command):

        if d == "ENCODER_PATH":
            newcom[n] = exe

        elif d == "IN":
            newcom[n] = filename

        elif d == "OUT":
            newcom[n] = outfn = base

        elif d == "OUT.EXT":
            newcom[n] = outfn = base + "." + enc_data['ext']

    returncode = subprocess.call(newcom)

    if returncode == 0 and g.delete_orig:
        os.unlink(filename)

    return outfn


def external_download(song, filename, url):
    """ Perform download using external application. """
    cmd = Config.DOWNLOAD_COMMAND.get
    ddir, basename = Config.DDIR.get, os.path.basename(filename)
    cmd_list = shlex.split(cmd)

    def list_string_sub(orig, repl, lst):
        """ Replace substrings for items in a list. """
        return [x if orig not in x else x.replace(orig, repl) for x in lst]

    cmd_list = list_string_sub("%F", filename, cmd_list)
    cmd_list = list_string_sub("%d", ddir, cmd_list)
    cmd_list = list_string_sub("%f", basename, cmd_list)
    cmd_list = list_string_sub("%u", url, cmd_list)
    cmd_list = list_string_sub("%i", song.ytid, cmd_list)
    dbg("Downloading using: %s", " ".join(cmd_list))
    subprocess.call(cmd_list)


def _download(song, filename, url=None, audio=False, allow_transcode=True):
    """ Download file, show status.

    Return filename or None in case of user specified download command.

    """
    # pylint: disable=R0914
    # too many local variables
    # Instance of 'bool' has no 'url' member (some types not inferable)

    if not url:
        stream = streams.select(streams.get(song), audio=audio, m4a_ok=True)
        url = stream['url']

    # if an external download command is set, use it
    if Config.DOWNLOAD_COMMAND.get:
        title = c.y + os.path.splitext(os.path.basename(filename))[0] + c.w
        xprint("Downloading %s using custom command" % title)
        external_download(song, filename, url)
        return None

    if not Config.OVERWRITE.get:
        if os.path.exists(filename):
            xprint("File exists. Skipping %s%s%s ..\n" % (c.r, filename, c.w))
            time.sleep(0.2)
            return filename

    xprint("Downloading to %s%s%s .." % (c.r, filename, c.w))
    status_string = ('  {0}{1:,}{2} Bytes [{0}{3:.2%}{2}] received. Rate: '
                     '[{0}{4:4.0f} kbps{2}].  ETA: [{0}{5:.0f} secs{2}]')

    resp = urlopen(url)
    total = int(resp.info()['Content-Length'].strip())
    chunksize, bytesdone, t0 = 16384, 0, time.time()
    outfh = open(filename, 'wb')

    while True:
        chunk = resp.read(chunksize)
        outfh.write(chunk)
        elapsed = time.time() - t0
        bytesdone += len(chunk)
        rate = (bytesdone / 1024) / elapsed
        eta = (total - bytesdone) / (rate * 1024)
        stats = (c.y, bytesdone, c.w, bytesdone * 1.0 / total, rate, eta)

        if not chunk:
            outfh.close()
            break

        status = status_string.format(*stats)
        sys.stdout.write("\r" + status + ' ' * 4 + "\r")
        sys.stdout.flush()

    active_encoder = g.encoders[Config.ENCODER.get]
    ext = filename.split(".")[-1]
    valid_ext = ext in active_encoder['valid'].split(",")

    if audio and g.muxapp:
        remux_audio(filename, song.title)

    if Config.ENCODER.get != 0 and valid_ext and allow_transcode:
        filename = transcode(filename, active_encoder)

    return filename


@commands.command(r'play\s+(%s|\d+)' % commands.word)
def play_pl(name):
    """ Play a playlist by name. """
    if name.isdigit():
        name = int(name)
        name = sorted(g.userpl)[name - 1]

    saved = g.userpl.get(name)

    if not saved:
        name = get_near_name(name, g.userpl)
        saved = g.userpl.get(name)

    if saved:
        g.model.songs = list(saved.songs)
        play_all("", "", "")

    else:
        g.message = F("pl not found") % name
        g.content = playlists_display()


@commands.command(r'save')
def save_last():
    """ Save command with no playlist name. """
    if g.last_opened:
        open_save_view("save", g.last_opened)

    else:
        saveas = ""

        # save using artist name in postion 1
        if g.model:
            saveas = g.model[0].title[:18].strip()
            saveas = re.sub(r"[^-\w]", "-", saveas, re.UNICODE)

        # loop to find next available name
        post = 0

        while g.userpl.get(saveas):
            post += 1
            saveas = g.model[0].title[:18].strip() + "-" + str(post)

        # Playlists are not allowed to start with a digit
        # TODO: Possibly change this, but ban purely numerical names
        saveas = saveas.lstrip("0123456789")

        open_save_view("save", saveas)

@commands.command(r'history')
def view_history():
    """ Display the user's play history """
    history = g.userhist.get('history')
    #g.last_opened = ""
    try:
        paginatesongs(list(reversed(history.songs)))
        g.message = "Viewing play history"

    except AttributeError:
        g.content = logo(c.r)
        g.message = "History empty"


@commands.command(r'history clear')
def clear_history():
    """ Clears the user's play history """
    g.userhist['history'].songs = []
    history.save()
    g.message = "History cleared"
    g.content = logo()


@commands.command(r'(open|save|view)\s*(%s)' % commands.word)
def open_save_view(action, name):
    """ Open, save or view a playlist by name.  Get closest name match. """
    name = name.replace(" ", "-")
    if action == "open" or action == "view":
        saved = g.userpl.get(name)

        if not saved:
            name = get_near_name(name, g.userpl)
            saved = g.userpl.get(name)

        elif action == "open":
            g.active.songs = list(saved.songs)
            g.last_opened = name
            msg = F("pl loaded") % name
            paginatesongs(g.active, msg=msg)

        elif action == "view":
            g.last_opened = ""
            msg = F("pl viewed") % name
            paginatesongs(list(saved.songs), msg=msg)

        elif not saved and action in "view open".split():
            g.message = F("pl not found") % name
            g.content = playlists_display()

    elif action == "save":
        if not g.model:
            g.message = "Nothing to save. " + F('advise search')
            g.content = generate_songlist_display()

        else:
            g.userpl[name] = Playlist(name, list(g.model.songs))
            g.message = F('pl saved') % name
            playlists.save()
            g.content = generate_songlist_display()


@commands.command(r'(open|view)\s*(\d{1,4})')
def open_view_bynum(action, num):
    """ Open or view a saved playlist by number. """
    srt = sorted(g.userpl)
    name = srt[int(num) - 1]
    open_save_view(action, name)


@commands.command(r'(rm|add)\s*(-?\d[-,\d\s]{,250})')
def songlist_rm_add(action, songrange):
    """ Remove or add tracks. works directly on user input. """
    selection = parse_multi(songrange)

    if action == "add":
        duplicate_songs = []
        for songnum in selection:
            if g.model[songnum - 1] in g.active:
                duplicate_songs.append(str(songnum))
            g.active.songs.append(g.model[songnum - 1])

        d = g.active.duration
        g.message = F('added to pl') % (len(selection), len(g.active), d)
        if duplicate_songs:
            duplicate_songs = ', '.join(sorted(duplicate_songs))
            g.message += '\n'
            g.message += F('duplicate tracks') % duplicate_songs

    elif action == "rm":
        selection = sorted(set(selection), reverse=True)
        removed = str(tuple(reversed(selection))).replace(",", "")

        for x in selection:
            g.model.songs.pop(x - 1)

        g.message = F('songs rm') % (len(selection), removed)

    g.content = generate_songlist_display()


@commands.command(r'(da|dv)\s+((?:\d+\s\d+|-\d|\d+-|\d,)(?:[\d\s,-]*))')
def down_many(dltype, choice, subdir=None):
    """ Download multiple items. """
    choice = parse_multi(choice)
    choice = list(set(choice))
    downsongs = [g.model[int(x) - 1] for x in choice]
    temp = g.model[::]
    g.model.songs = downsongs[::]
    count = len(downsongs)
    av = "audio" if dltype.startswith("da") else "video"
    msg = ""

    def handle_error(message):
        """ Handle error in download. """
        g.message = message
        g.content = disp
        screen.update()
        time.sleep(2)
        g.model.songs.pop(0)

    try:
        for song in downsongs:
            g.result_count = len(g.model)
            disp = generate_songlist_display()
            title = "Download Queue (%s):%s\n\n" % (av, c.w)
            disp = re.sub(r"(Num\s*?Title.*?\n)", title, disp)
            g.content = disp
            screen.update()

            try:
                filename = _make_fname(song, None, av=av, subdir=subdir)

            except IOError as e:
                handle_error("Error for %s: %s" % (song.title, str(e)))
                count -= 1
                continue

            except KeyError:
                handle_error("No audio track for %s" % song.title)
                count -= 1
                continue

            try:
                _download(song, filename, url=None, audio=av == "audio")

            except HTTPError:
                handle_error("HTTP Error for %s" % song.title)
                count -= 1
                continue

            g.model.songs.pop(0)
            msg = "Downloaded %s items" % count
            g.message = "Saved to " + c.g + song.title + c.w

    except KeyboardInterrupt:
        msg = "Downloads interrupted!"

    finally:
        g.model.songs = temp[::]
        g.message = msg
        g.result_count = len(g.model)
        g.content = generate_songlist_display()


@commands.command(r'(da|dv)pl\s+%s' % commands.pl)
def down_plist(dltype, parturl):
    """ Download YouTube playlist. """

    plist(parturl)
    dump(False)
    title = g.pafy_pls[parturl][0].title
    subdir = mswinfn(title.replace("/", "-"))
    down_many(dltype, "1-", subdir=subdir)
    msg = g.message
    plist(parturl)
    g.message = msg


@commands.command(r'(da|dv)upl\s+(.*)')
def down_user_pls(dltype, user):
    """ Download all user playlists. """
    user_pls(user)
    for pl in g.ytpls:
        down_plist(dltype, pl.get('link'))

    return


@commands.command(r'(%s{0,3})([-,\d\s]{1,250})\s*(%s{0,3})$' %
        (commands.rs, commands.rs))
def play(pre, choice, post=""):
    """ Play choice.  Use repeat/random if appears in pre/post. """
    # pylint: disable=R0914
    # too many local variables

    if g.browse_mode == "ytpl":

        if choice.isdigit():
            return plist(g.ytpls[int(choice) - 1]['link'])
        else:
            g.message = "Invalid playlist selection: %s" % c.y + choice + c.w
            g.content = generate_songlist_display()
            return

    if not g.model:
        g.message = c.r + "There are no tracks to select" + c.w
        g.content = g.content or generate_songlist_display()

    else:
        shuffle = "shuffle" in pre + post
        repeat = "repeat" in pre + post
        novid = "-a" in pre + post
        fs = "-f" in pre + post
        nofs = "-w" in pre + post
        forcevid = "-v" in pre + post

        if ((novid and fs) or (novid and nofs) or (nofs and fs)
           or (novid and forcevid)):
            raise IOError("Conflicting override options specified")

        override = False
        override = "audio" if novid else override
        override = "fullscreen" if fs else override
        override = "window" if nofs else override

        if (not fs) and (not nofs):
            override = "forcevid" if forcevid else override

        selection = parse_multi(choice)
        songlist = [g.model[x - 1] for x in selection]

        # cache next result of displayed items
        # when selecting a single item
        if len(songlist) == 1:
            chosen = selection[0] - 1

            if len(g.model) > chosen + 1:
                streams.preload(g.model[chosen + 1], override=override)

        play_range(songlist, shuffle, repeat, override)
        g.content = generate_songlist_display()


@commands.command(r'(%s{0,3})(?:\*|all)\s*(%s{0,3})' %
        (commands.rs, commands.rs))
def play_all(pre, choice, post=""):
    """ Play all tracks in model (last displayed). shuffle/repeat if req'd."""
    options = pre + choice + post
    play(options, "1-" + str(len(g.model)))


@commands.command(r'ls')
def ls():
    """ List user saved playlists. """
    if not g.userpl:
        g.message = F('no playlists')
        g.content = g.content or generate_songlist_display(zeromsg=g.message)

    else:
        g.content = playlists_display()
        g.message = F('pl help')


@commands.command(r'vp')
def vp():
    """ View current working playlist. """

    msg = F('current pl')
    txt = F('advise add') if g.model else F('advise search')
    failmsg = F('pl empty') + " " + txt

    paginatesongs(g.active, msg=msg, failmsg=failmsg)


@commands.command(r'(?:help|h)(?:\s+([-_a-zA-Z]+))?')
def show_help(choice):
    """ Print help message. """

    g.content = get_help(choice)


@commands.command(r'(?:q|quit|exit)')
def quits(showlogo=True):
    """ Exit the program. """
    if has_readline:
        readline.write_history_file(g.READLINE_FILE)
        dbg("Saved history file")

    cache.save()

    screen.clear()
    msg = logo(c.r, version=__version__) if showlogo else ""
    msg += F("exitmsg", 2)

    if Config.CHECKUPDATE.get and showlogo:

        try:
            url = "https://raw.githubusercontent.com/mps-youtube/mps-youtube/master/VERSION"
            v = urlopen(url, timeout=1).read().decode()
            v = re.search(r"^version\s*([\d\.]+)\s*$", v, re.MULTILINE)

            if v:
                v = v.group(1)

                if v > __version__:
                    msg += "\n\nA newer version is available (%s)\n" % v

        except (URLError, HTTPError, socket.timeout):
            dbg("check update timed out")

    screen.msgexit(msg)


def get_dl_data(song, mediatype="any"):
    """ Get filesize and metadata for all streams, return dict. """
    def mbsize(x):
        """ Return size in MB. """
        return str(int(x / (1024 ** 2)))

    p = get_pafy(song)
    dldata = []
    text = " [Fetching stream info] >"
    streamlist = [x for x in p.allstreams]

    if mediatype == "audio":
        streamlist = [x for x in p.audiostreams]

    l = len(streamlist)
    for n, stream in enumerate(streamlist):
        sys.stdout.write(text + "-" * n + ">" + " " * (l - n - 1) + "<\r")
        sys.stdout.flush()

        try:
            size = mbsize(stream.get_filesize())

        except TypeError:
            dbg(c.r + "---Error getting stream size" + c.w)
            size = 0

        item = {'mediatype': stream.mediatype,
                'size': size,
                'ext': stream.extension,
                'quality': stream.quality,
                'notes': stream.notes,
                'url': stream.url}

        dldata.append(item)

    screen.writestatus("")
    return dldata, p


def menu_prompt(model, prompt=" > ", rows=None, header=None, theading=None,
                footer=None, force=0):
    """ Generate a list of choice, returns item from model. """
    content = ""

    for x in header, theading, rows, footer:
        if isinstance(x, list):

            for line in x:
                content += line + "\n"

        elif isinstance(x, str):
            content += x + "\n"

    g.content = content
    screen.update()

    choice = input(prompt)

    if choice in model:
        return model[choice]

    elif force:
        return menu_prompt(model, prompt, rows, header, theading, footer,
                           force)

    elif not choice.strip():
        return False, False

    else:  # unrecognised input
        return False, "abort"


def prompt_dl(song):
    """ Prompt user do choose a stream to dl.  Return (url, extension). """
    # pylint: disable=R0914
    dl_data, p = get_dl_data(song)
    dl_text = gen_dl_text(dl_data, song, p)

    model = [x['url'] for x in dl_data]
    ed = enumerate(dl_data)
    model = {str(n + 1): (x['url'], x['ext']) for n, x in ed}
    url, ext = menu_prompt(model, "Download number: ", *dl_text)
    url2 = ext2 = None
    mediatype = [i for i in dl_data if i['url'] == url][0]['mediatype']

    if mediatype == "video" and g.muxapp and not Config.DOWNLOAD_COMMAND.get:
        # offer mux if not using external downloader
        dl_data, p = get_dl_data(song, mediatype="audio")
        dl_text = gen_dl_text(dl_data, song, p)
        au_choices = "1" if len(dl_data) == 1 else "1-%s" % len(dl_data)
        footer = [F('-audio') % ext, F('select mux') % au_choices]
        dl_text = tuple(dl_text[0:3]) + (footer,)
        aext = ("ogg", "m4a")
        model = [x['url'] for x in dl_data if x['ext'] in aext]
        ed = enumerate(dl_data)
        model = {str(n + 1): (x['url'], x['ext']) for n, x in ed}
        prompt = "Audio stream: "
        url2, ext2 = menu_prompt(model, prompt, *dl_text)

    return url, ext, url2, ext2


def gen_dl_text(ddata, song, p):
    """ Generate text for dl screen. """
    hdr = []
    hdr.append("  %s%s%s" % (c.r, song.title, c.w))
    author = p.author
    hdr.append(c.r + "  Uploaded by " + author + c.w)
    hdr.append("  [" + fmt_time(song.length) + "]")
    hdr.append("")

    heading = tuple("Item Format Quality Media Size Notes".split())
    fmt = "  {0}%-6s %-8s %-13s %-7s   %-5s   %-16s{1}"
    heading = [fmt.format(c.w, c.w) % heading]
    heading.append("")

    content = []

    for n, d in enumerate(ddata):
        row = (n + 1, d['ext'], d['quality'], d['mediatype'], d['size'],
               d['notes'])
        fmt = "  {0}%-6s %-8s %-13s %-7s %5s Mb   %-16s{1}"
        row = fmt.format(c.g, c.w) % row
        content.append(row)

    content.append("")

    footer = "Select [%s1-%s%s] to download or [%sEnter%s] to return"
    footer = [footer % (c.y, len(content) - 1, c.w, c.y, c.w)]
    return(content, hdr, heading, footer)


@commands.command(r'(dv|da|d|dl|download)\s*(\d{1,4})')
def download(dltype, num):
    """ Download a track or playlist by menu item number. """
    # This function needs refactoring!
    # pylint: disable=R0912
    # pylint: disable=R0914
    if g.browse_mode == "ytpl" and dltype in ("da", "dv"):
        plid = g.ytpls[int(num) - 1]["link"]
        down_plist(dltype, plid)
        return

    elif g.browse_mode == "ytpl":
        g.message = "Use da or dv to specify audio / video playlist download"
        g.message = c.y + g.message + c.w
        g.content = generate_songlist_display()
        return

    elif g.browse_mode != "normal":
        g.message = "Download must refer to a specific video item"
        g.message = c.y + g.message + c.w
        g.content = generate_songlist_display()
        return

    screen.writestatus("Fetching video info...")
    song = (g.model[int(num) - 1])
    best = dltype.startswith("dv") or dltype.startswith("da")

    if not best:

        try:
            # user prompt for download stream
            url, ext, url_au, ext_au = prompt_dl(song)

        except KeyboardInterrupt:
            g.message = c.r + "Download aborted!" + c.w
            g.content = generate_songlist_display()
            return

        if not url or ext_au == "abort":
            # abort on invalid stream selection
            g.content = generate_songlist_display()
            g.message = "%sNo download selected / invalid input%s" % (c.y, c.w)
            return

        else:
            # download user selected stream(s)
            filename = _make_fname(song, ext)
            args = (song, filename, url)

            if url_au and ext_au:
                # downloading video and audio stream for muxing
                audio = False
                filename_au = _make_fname(song, ext_au)
                args_au = (song, filename_au, url_au)

            else:
                audio = ext in ("m4a", "ogg")

            kwargs = dict(audio=audio)

    elif best:
        # set updownload without prompt
        url_au = None
        av = "audio" if dltype.startswith("da") else "video"
        audio = av == "audio"
        filename = _make_fname(song, None, av=av)
        args = (song, filename)
        kwargs = dict(url=None, audio=audio)

    try:
        # perform download(s)
        dl_filenames = [args[1]]
        f = _download(*args, **kwargs)
        if f:
            g.message = "Saved to " + c.g + f + c.w

        if url_au:
            dl_filenames += [args_au[1]]
            _download(*args_au, allow_transcode=False, **kwargs)

    except KeyboardInterrupt:
        g.message = c.r + "Download halted!" + c.w

        try:
            for downloaded in dl_filenames:
                os.remove(downloaded)

        except IOError:
            pass

    if url_au:
        # multiplex
        name, ext = os.path.splitext(args[1])
        tmpvideoname = name + '.' +str(random.randint(10000, 99999)) + ext
        os.rename(args[1], tmpvideoname)
        mux_cmd = [g.muxapp, "-i", tmpvideoname, "-i", args_au[1], "-c",
                   "copy", name + ".mp4"]

        try:
            subprocess.call(mux_cmd)
            g.message = "Saved to :" + c.g + mux_cmd[7] + c.w
            os.remove(tmpvideoname)
            os.remove(args_au[1])

        except KeyboardInterrupt:
            g.message = "Audio/Video multiplex aborted!"

    g.content = generate_songlist_display()


def prompt_for_exit():
    """ Ask for exit confirmation. """
    g.message = c.r + "Press ctrl-c again to exit" + c.w
    g.content = generate_songlist_display()
    screen.update()

    try:
        userinput = input(c.r + " > " + c.w)

    except (KeyboardInterrupt, EOFError):
        quits(showlogo=False)

    return userinput


@commands.command(r'rmp\s*(\d+|%s)' % commands.word)
def playlist_remove(name):
    """ Delete a saved playlist by name - or purge working playlist if *all."""
    if name.isdigit() or g.userpl.get(name):

        if name.isdigit():
            name = int(name) - 1
            name = sorted(g.userpl)[name]

        del g.userpl[name]
        g.message = "Deleted playlist %s%s%s" % (c.y, name, c.w)
        g.content = playlists_display()
        playlists.save()

    else:
        g.message = F('pl not found advise ls') % name
        g.content = playlists_display()


@commands.command(r'(mv|sw)\s*(\d{1,4})\s*[\s,]\s*(\d{1,4})')
def songlist_mv_sw(action, a, b):
    """ Move a song or swap two songs. """
    i, j = int(a) - 1, int(b) - 1

    if action == "mv":
        g.model.songs.insert(j, g.model.songs.pop(i))
        g.message = F('song move') % (g.model[j].title, b)

    elif action == "sw":
        g.model[i], g.model[j] = g.model[j], g.model[i]
        g.message = F('song sw') % (min(a, b), max(a, b))

    g.content = generate_songlist_display()


@commands.command(r'add\s*(-?\d[-,\d\s]{1,250})(%s)' % commands.word)
def playlist_add(nums, playlist):
    """ Add selected song nums to saved playlist. """
    nums = parse_multi(nums)

    if not g.userpl.get(playlist):
        playlist = playlist.replace(" ", "-")
        g.userpl[playlist] = Playlist(playlist)

    for songnum in nums:
        g.userpl[playlist].songs.append(g.model[songnum - 1])
        dur = g.userpl[playlist].duration
        f = (len(nums), playlist, len(g.userpl[playlist]), dur)
        g.message = F('added to saved pl') % f

    if nums:
        playlists.save()

    g.content = generate_songlist_display()


@commands.command(r'mv\s*(\d{1,3})\s*(%s)' % commands.word)
def playlist_rename_idx(_id, name):
    """ Rename a playlist by ID. """
    _id = int(_id) - 1
    playlist_rename(sorted(g.userpl)[_id] + " " + name)


@commands.command(r'mv\s*(%s\s+%s)' % (commands.word, commands.word))
def playlist_rename(playlists):
    """ Rename a playlist using mv command. """
    # Deal with old playlist names that permitted spaces
    a, b = "", playlists.split(" ")
    while a not in g.userpl:
        a = (a + " " + (b.pop(0))).strip()
        if not b and a not in g.userpl:
            g.message = F('no pl match for rename')
            g.content = g.content or playlists_display()
            return

    b = "-".join(b)
    g.userpl[b] = Playlist(b)
    g.userpl[b].songs = list(g.userpl[a].songs)
    playlist_remove(a)
    g.message = F('pl renamed') % (a, b)
    playlists.save()


@commands.command(r'(rm|add)\s(?:\*|all)')
def add_rm_all(action):
    """ Add all displayed songs to current playlist.

    remove all displayed songs from view.

    """
    if action == "rm":
        g.model.songs.clear()
        msg = c.b + "Cleared all songs" + c.w
        g.content = generate_songlist_display(zeromsg=msg)

    elif action == "add":
        size = len(g.model)
        songlist_rm_add("add", "-" + str(size))


@commands.command(r'(n|p)\s*(\d{1,2})?')
def nextprev(np, page=None):
    """ Get next / previous search results. """
    if isinstance(g.content, content.PaginatedContent):
        page_count = g.content.numPages()
        function = g.content.getPage
        args = {}
    else:
        page_count = math.ceil(g.result_count/getxy().max_results)
        function, args = g.last_search_query

    good = False

    if function:
        if np == "n":
            if g.current_page + 1 < page_count:
                g.current_page += 1
                good = True

        elif np == "p":
            if page and int(page) in range(1,20):
                g.current_page = int(page)-1
                good = True

            elif g.current_page > 0:
                g.current_page -= 1
                good = True

    if good:
        function(page=g.current_page, **args)

    else:
        norp = "next" if np == "n" else "previous"
        g.message = "No %s items to display" % norp

    if not isinstance(g.content, content.PaginatedContent):
        g.content = generate_songlist_display()
    return good


@commands.command(r'u\s?([\d]{1,4})')
def user_more(num):
    """ Show more videos from user of vid num. """
    if g.browse_mode != "normal":
        g.message = "User uploads must refer to a specific video item"
        g.message = c.y + g.message + c.w
        g.content = generate_songlist_display()
        return

    g.current_page = 0
    item = g.model[int(num) - 1]
    channel_id = g.meta.get(item.ytid, {}).get('uploader')
    user = g.meta.get(item.ytid, {}).get('uploaderName')
    usersearch_id(user, channel_id, '')


@commands.command(r'r\s?(\d{1,4})')
def related(num):
    """ Show videos related to to vid num. """
    if g.browse_mode != "normal":
        g.message = "Related items must refer to a specific video item"
        g.message = c.y + g.message + c.w
        g.content = generate_songlist_display()
        return

    g.current_page = 0
    item = g.model[int(num) - 1]
    related_search(item)


@commands.command(r'x\s*(\d+)')
def clip_copy(num):
    """ Copy item to clipboard. """
    if g.browse_mode == "ytpl":

        p = g.ytpls[int(num) - 1]
        link = "https://youtube.com/playlist?list=%s" % p['link']

    elif g.browse_mode == "normal":
        item = (g.model[int(num) - 1])
        link = "https://youtube.com/watch?v=%s" % item.ytid

    else:
        g.message = "clipboard copy not valid in this mode"
        g.content = generate_songlist_display()
        return

    if has_pyperclip:

        try:
            pyperclip.copy(link)
            g.message = c.y + link + c.w + " copied"
            g.content = generate_songlist_display()

        except Exception as e:
            g.content = generate_songlist_display()
            g.message = link + "\nError - couldn't copy to clipboard.\n" + \
                    ''.join(traceback.format_exception_only(type(e), e))

    else:
        g.message = "pyperclip module must be installed for clipboard support\n"
        g.message += "see https://pypi.python.org/pypi/pyperclip/"
        g.content = generate_songlist_display()


@commands.command(r'mix\s*(\d{1,4})')
def mix(num):
    """ Retrieves the YouTube mix for the selected video. """
    g.content = g.content or generate_songlist_display()
    if g.browse_mode != "normal":
        g.message = F('mix only videos')
    else:
        item = (g.model[int(num) - 1])
        if item is None:
            g.message = F('invalid item')
            return
        item = get_pafy(item)
        # Mix playlists are made up of 'RD' + video_id
        try:
            plist("RD" + item.videoid)
        except OSError:
            g.message = F('no mix')


@commands.command(r'i\s*(\d{1,4})')
def info(num):
    """ Get video description. """
    if g.browse_mode == "ytpl":
        p = g.ytpls[int(num) - 1]

        # fetch the playlist item as it has more metadata
        if p['link'] in g.pafy_pls:
            ytpl = g.pafy_pls[p['link']][0]
        else:
            g.content = logo(col=c.g)
            g.message = "Fetching playlist info.."
            screen.update()
            dbg("%sFetching playlist using pafy%s", c.y, c.w)
            ytpl = pafy.get_playlist2(p['link'])
            g.pafy_pls[p['link']] = (ytpl, IterSlicer(ytpl))

        ytpl_desc = ytpl.description
        g.content = generate_songlist_display()

        created = yt_datetime(p['created'])[0]
        updated = yt_datetime(p['updated'])[0]
        out = c.ul + "Playlist Info" + c.w + "\n\n"
        out += p['title']
        out += "\n" + ytpl_desc
        out += ("\n\nAuthor     : " + p['author'])
        out += "\nSize       : " + str(p['size']) + " videos"
        out += "\nCreated    : " + time.strftime("%x %X", created)
        out += "\nUpdated    : " + time.strftime("%x %X", updated)
        out += "\nID         : " + str(p['link'])
        out += ("\n\n%s[%sPress enter to go back%s]%s" % (c.y, c.w, c.y, c.w))
        g.content = out

    elif g.browse_mode == "normal":
        g.content = logo(c.b)
        screen.update()
        screen.writestatus("Fetching video metadata..")
        item = (g.model[int(num) - 1])
        streams.get(item)
        p = get_pafy(item)
        pub = time.strptime(str(p.published), "%Y-%m-%d %H:%M:%S")
        screen.writestatus("Fetched")
        out = c.ul + "Video Info" + c.w + "\n\n"
        out += p.title or ""
        out += "\n" + (p.description or "")
        out += "\n\nAuthor     : " + str(p.author)
        out += "\nPublished  : " + time.strftime("%c", pub)
        out += "\nView count : " + str(p.viewcount)
        out += "\nRating     : " + str(p.rating)[:4]
        out += "\nLikes      : " + str(p.likes)
        out += "\nDislikes   : " + str(p.dislikes)
        out += "\nCategory   : " + str(p.category)
        out += "\nLink       : " + "https://youtube.com/watch?v=%s" % p.videoid
        out += "\n\n%s[%sPress enter to go back%s]%s" % (c.y, c.w, c.y, c.w)
        g.content = out


@commands.command(r'playurl\s(.*[-_a-zA-Z0-9]{11}[^\s]*)(\s-(?:f|a|w))?')
def play_url(url, override):
    """ Open and play a youtube video url. """
    override = override if override else "_"
    g.browse_mode = "normal"
    yt_url(url, print_title=1)

    if len(g.model) == 1:
        play(override, "1", "_")

    if g.command_line:
        sys.exit()


@commands.command(r'browserplay\s(\d{1,50})')
def browser_play(number):
    """Open a previously searched result in the browser."""
    if (len(g.model) == 0):
        g.message = c.r + "No previous search." + c.w
        g.content = logo(c.r)
        return

    try:
        index = int(number) - 1

        if (0 <= index < len(g.model)):
            base_url = "https://www.youtube.com/watch?v="
            video = g.model[index]
            url = base_url + video.ytid
            webbrowser.open(url)
            g.content = g.content or generate_songlist_display()

        else:
            g.message = c.r + "Out of range." + c.w
            g.content = g.content or generate_songlist_display()
            return

    except (HTTPError, URLError, Exception) as e:
        g.message = c.r + str(e) + c.w
        g.content = g.content or generate_songlist_display()
        return


@commands.command(r'dlurl\s(.*[-_a-zA-Z0-9]{11}.*)')
def dl_url(url):
    """ Open and prompt for download of youtube video url. """
    g.browse_mode = "normal"
    yt_url(url)

    if len(g.model) == 1:
        download("download", "1")

    if g.command_line:
        sys.exit()


@commands.command(r'daurl\s(.*[-_a-zA-Z0-9]{11}.*)')
def da_url(url):
    """ Open and prompt for download of youtube best audio from url. """
    g.browse_mode = "normal"
    yt_url(url)

    if len(g.model) == 1:
        download("da", "1")

    if g.command_line:
        sys.exit()


@commands.command(r'url\s(.*[-_a-zA-Z0-9]{11}.*)')
def yt_url(url, print_title=0):
    """ Acess videos by urls. """
    url_list = url.split()

    g.model.songs = []

    for u in url_list:
        try:
            p = pafy.new(u)

        except (IOError, ValueError) as e:
            g.message = c.r + str(e) + c.w
            g.content = g.content or generate_songlist_display(zeromsg=g.message)
            return

        g.browse_mode = "normal"
        v = Video(p.videoid, p.title, p.length)
        g.model.songs.append(v)

    if not g.command_line:
        g.content = generate_songlist_display()

    if print_title:
        xprint(v.title)


@commands.command(r'url_file\s(\S+)')
def yt_url_file(file_name):
    """ Access a list of urls in a text file """

    #Open and read the file
    try:
        with open(file_name, "r") as fo:
            output = ' '.join([line.strip() for line in fo if line.strip()])

    except (IOError):
        g.message = c.r + 'Error while opening the file, check the validity of the path' + c.w
        g.content = g.content or generate_songlist_display(zeromsg=g.message)
        return

    #Finally pass the input to yt_url
    yt_url(output)


@commands.command(r'(un)?dump')
def dump(un):
    """ Show entire playlist. """
    func, args = g.last_search_query

    if func is paginatesongs:
        paginatesongs(dumps=(not un), **args)

    else:
        un = "" if not un else un
        g.message = "%s%sdump%s may only be used on an open YouTube playlist"
        g.message = g.message % (c.y, un, c.w)
        g.content = generate_songlist_display()


def paginatesongs(func, page=0, splash=True, dumps=False,
        length=None, msg=None, failmsg=None, loadmsg=None):
    if splash:
        g.message = loadmsg or ''
        g.content = logo(col=c.b)
        screen.update()

    max_results = getxy().max_results

    if dumps:
        s = 0
        e = None
    else:
        s = page * max_results
        e = (page + 1) * max_results

    if callable(func):
        songs = func(s, e)
    else:
        songs = func[s:e]

    if length is None:
        length = len(func)

    args = {'func':func, 'length':length, 'msg':msg,
            'failmsg':failmsg, 'loadmsg': loadmsg}
    g.last_search_query = (paginatesongs, args)
    g.browse_mode = "normal"
    g.current_page = page
    g.result_count = length
    g.model.songs = songs
    g.content = generate_songlist_display()
    g.last_opened = ""
    g.message = msg or ''
    if not songs:
        g.message = failmsg or g.message

    if songs:
        # preload first result url
        streams.preload(songs[0], delay=0)


@commands.command(r'pl\s+%s' % commands.pl)
def plist(parturl):
    """ Retrieve YouTube playlist. """

    if parturl in g.pafy_pls:
        ytpl, plitems = g.pafy_pls[parturl]
    else:
        dbg("%sFetching playlist using pafy%s", c.y, c.w)
        ytpl = pafy.get_playlist2(parturl)
        plitems = IterSlicer(ytpl)
        g.pafy_pls[parturl] = (ytpl, plitems)

    def pl_seg(s, e):
        return [Video(i.videoid, i.title, i.length) for i in plitems[s:e]]

    msg = "Showing YouTube playlist %s" % (c.y + ytpl.title + c.w)
    loadmsg = "Retrieving YouTube playlist"
    paginatesongs(pl_seg, length=len(ytpl), msg=msg, loadmsg=loadmsg)


@commands.command(r'shuffle')
def shuffle_fn():
    """ Shuffle displayed items. """
    random.shuffle(g.model.songs)
    g.message = c.y + "Items shuffled" + c.w
    g.content = generate_songlist_display()


@commands.command(r'reverse')
def reverse_songs():
    """ Reverse order of displayed items. """
    g.model.songs = g.model.songs[::-1]
    g.message = c.y + "Reversed displayed songs" + c.w
    g.content = generate_songlist_display()


@commands.command(r'reverse\s*(\d{1,4})\s*-\s*(\d{1,4})\s*')
def reverse_songs_range(lower, upper):
    """ Reverse the songs within a specified range. """
    lower, upper = int(lower), int(upper)
    if lower > upper: lower, upper = upper, lower
    
    g.model.songs[lower-1:upper] = reversed(g.model.songs[lower-1:upper])
    g.message = c.y + "Reversed range: " + str(lower) + "-" + str(upper) + c.w
    g.content = generate_songlist_display()
    

@commands.command(r'reverse all')
def reverse_playlist():
    """ Reverse order of entire loaded playlist. """
    # Prevent crash if no last query
    if g.last_search_query == (None, None) or \
            'func' not in g.last_search_query[1]:
        g.content = logo()
        g.message = "No playlist loaded"
        return

    songs_list_or_func = g.last_search_query[1]['func']
    if callable(songs_list_or_func):
        songs = reversed(songs_list_or_func(0,None))
    else:
        songs = reversed(songs_list_or_func)

    paginatesongs(list(songs))
    g.message = c.y + "Reversed entire playlist" + c.w
    g.content = generate_songlist_display()


@commands.command(r'clearcache')
def clearcache():
    """ Clear cached items - for debugging use. """
    g.pafs = {}
    g.streams = {}
    dbg("%scache cleared%s", c.p, c.w)
    g.message = "cache cleared"


def show_message(message, col=c.r, update=False):
    """ Show message using col, update screen if required. """
    g.content = generate_songlist_display()
    g.message = col + message + c.w

    if update:
        screen.update()


def _do_query(url, query, err='query failed', report=False):
    """ Perform http request using mpsyt user agent header.

    if report is True, return whether response is from memo

    """
    # create url opener
    ua = "mps-youtube/%s ( %s )" % (__version__, __url__)
    mpsyt_opener = build_opener()
    mpsyt_opener.addheaders = [('User-agent', ua)]

    # convert query to sorted list of tuples (needed for consistent url_memo)
    query = [(k, query[k]) for k in sorted(query.keys())]
    url = "%s?%s" % (url, urlencode(query))

    try:
        wdata = mpsyt_opener.open(url).read().decode()

    except (URLError, HTTPError) as e:
        g.message = "%s: %s (%s)" % (err, e, url)
        g.content = logo(c.r)
        return None if not report else (None, False)

    return wdata if not report else (wdata, False)


def _best_song_match(songs, title, duration):
    """ Select best matching song based on title, length.

    Score from 0 to 1 where 1 is best.

    """
    # pylint: disable=R0914
    seqmatch = difflib.SequenceMatcher

    def variance(a, b):
        """ Return difference ratio. """
        return float(abs(a - b)) / max(a, b)

    candidates = []

    ignore = "music video lyrics new lyrics video audio".split()
    extra = "official original vevo".split()

    for song in songs:
        dur, tit = int(song.length), song.title
        dbg("Title: %s, Duration: %s", tit, dur)

        for word in extra:
            if word in tit.lower() and word not in title.lower():
                pattern = re.compile(word, re.I)
                tit = pattern.sub("", tit)

        for word in ignore:
            if word in tit.lower() and word not in title.lower():
                pattern = re.compile(word, re.I)
                tit = pattern.sub("", tit)

        replacechars = re.compile(r"[\]\[\)\(\-]")
        tit = replacechars.sub(" ", tit)
        multiple_spaces = re.compile(r"(\s)(\s*)")
        tit = multiple_spaces.sub(r"\1", tit)

        title_score = seqmatch(None, title.lower(), tit.lower()).ratio()
        duration_score = 1 - variance(duration, dur)
        dbg("Title score: %s, Duration score: %s", title_score,
            duration_score)

        # apply weightings
        score = duration_score * .5 + title_score * .5
        candidates.append((score, song))

    best_score, best_song = max(candidates, key=lambda x: x[0])
    percent_score = int(100 * best_score)
    return best_song, percent_score


def _match_tracks(artist, title, mb_tracks):
    """ Match list of tracks in mb_tracks by performing multiple searches. """
    # pylint: disable=R0914
    dbg("artists is %s", artist)
    dbg("title is %s", title)
    title_artist_str = c.g + title + c.w, c.g + artist + c.w
    xprint("\nSearching for %s by %s\n\n" % title_artist_str)

    def dtime(x):
        """ Format time to M:S. """
        return time.strftime('%M:%S', time.gmtime(int(x)))

    # do matching
    for track in mb_tracks:
        ttitle = track['title']
        length = track['length']
        xprint("Search :  %s%s - %s%s - %s" % (c.y, artist, ttitle, c.w,
                                               dtime(length)))
        q = "%s %s" % (artist, ttitle)
        w = q = ttitle if artist == "Various Artists" else q
        query = generate_search_qs(w, 0)
        dbg(query)

        # perform fetch
        wdata = call_gdata('search', query)
        results = get_tracks_from_json(wdata)

        if not results:
            xprint(c.r + "Nothing matched :(\n" + c.w)
            continue

        s, score = _best_song_match(results, artist + " " + ttitle, length)
        cc = c.g if score > 85 else c.y
        cc = c.r if score < 75 else cc
        xprint("Matched:  %s%s%s - %s \n[%sMatch confidence: "
               "%s%s]\n" % (c.y, s.title, c.w, fmt_time(s.length),
                            cc, score, c.w))
        yield s


def _get_mb_tracks(albumid):
    """ Get track listing from MusicBraiz by album id. """
    ns = {'mb': 'http://musicbrainz.org/ns/mmd-2.0#'}
    url = "http://musicbrainz.org/ws/2/release/" + albumid
    query = {"inc": "recordings"}
    wdata = _do_query(url, query, err='album search error')

    if not wdata:
        return None

    root = ET.fromstring(wdata)
    tlist = root.find("./mb:release/mb:medium-list/mb:medium/mb:track-list",
                      namespaces=ns)
    mb_songs = tlist.findall("mb:track", namespaces=ns)
    tracks = []
    path = "./mb:recording/mb:"

    for track in mb_songs:

        try:
            title, length, rawlength = "unknown", 0, 0
            title = track.find(path + "title", namespaces=ns).text
            rawlength = track.find(path + "length", namespaces=ns).text
            length = int(round(float(rawlength) / 1000))

        except (ValueError, AttributeError):
            xprint("not found")

        tracks.append(dict(title=title, length=length, rawlength=rawlength))

    return tracks


def _get_mb_album(albumname, **kwa):
    """ Return artist, album title and track count from MusicBrainz. """
    url = "http://musicbrainz.org/ws/2/release/"
    qargs = dict(
        release='"%s"' % albumname,
        primarytype=kwa.get("primarytype", "album"),
        status=kwa.get("status", "official"))
    qargs.update({k: '"%s"' % v for k, v in kwa.items()})
    qargs = ["%s:%s" % item for item in qargs.items()]
    qargs = {"query": " AND ".join(qargs)}
    g.message = "Album search for '%s%s%s'" % (c.y, albumname, c.w)
    wdata = _do_query(url, qargs)

    if not wdata:
        return None

    ns = {'mb': 'http://musicbrainz.org/ns/mmd-2.0#'}
    root = ET.fromstring(wdata)
    rlist = root.find("mb:release-list", namespaces=ns)

    if int(rlist.get('count')) == 0:
        return None

    album = rlist.find("mb:release", namespaces=ns)
    artist = album.find("./mb:artist-credit/mb:name-credit/mb:artist",
                        namespaces=ns).find("mb:name", namespaces=ns).text
    title = album.find("mb:title", namespaces=ns).text
    aid = album.get('id')
    return dict(artist=artist, title=title, aid=aid)


@commands.command(r'album\s*(.{0,500})')
def search_album(term):
    """Search for albums. """
    # pylint: disable=R0914,R0912
    if not term:
        show_message("Enter album name:", c.g, update=True)
        term = input("> ")

        if not term or len(term) < 2:
            g.message = c.r + "Not enough input!" + c.w
            g.content = generate_songlist_display()
            return

    album = _get_mb_album(term)

    if not album:
        show_message("Album '%s' not found!" % term)
        return

    out = "'%s' by %s%s%s\n\n" % (album['title'],
                                  c.g, album['artist'], c.w)
    out += ("[Enter] to continue, [q] to abort, or enter artist name for:\n"
            "    %s" % (c.y + term + c.w + "\n"))

    prompt = "Artist? [%s] > " % album['artist']
    xprint(prompt, end="")
    artistentry = input().strip()

    if artistentry:

        if artistentry == "q":
            show_message("Album search abandoned!")
            return

        album = _get_mb_album(term, artist=artistentry)

        if not album:
            show_message("Album '%s' by '%s' not found!" % (term, artistentry))
            return

    title, artist = album['title'], album['artist']
    mb_tracks = _get_mb_tracks(album['aid'])

    if not mb_tracks:
        show_message("Album '%s' by '%s' has 0 tracks!" % (title, artist))
        return

    msg = "%s%s%s by %s%s%s\n\n" % (c.g, title, c.w, c.g, artist, c.w)
    msg += "Enter to begin matching or [q] to abort"
    g.message = msg
    g.content = "Tracks:\n"
    for n, track in enumerate(mb_tracks, 1):
        g.content += "%02s  %s" % (n, track['title'])
        g.content += "\n"

    screen.update()
    entry = input("Continue? [Enter] > ")

    if entry == "":
        pass

    else:
        show_message("Album search abandoned!")
        return

    songs = []
    screen.clear()
    itt = _match_tracks(artist, title, mb_tracks)

    stash = Config.SEARCH_MUSIC.get, Config.ORDER.get
    Config.SEARCH_MUSIC.value = True
    Config.ORDER.value = "relevance"

    try:
        songs.extend(itt)

    except KeyboardInterrupt:
        xprint("%sHalted!%s" % (c.r, c.w))

    finally:
        Config.SEARCH_MUSIC.value, Config.ORDER.value = stash

    if songs:
        xprint("\n%s / %s songs matched" % (len(songs), len(mb_tracks)))
        input("Press Enter to continue")

    msg =  "Contents of album %s%s - %s%s %s(%d/%d)%s:" % (
            c.y, artist, title, c.w, c.b, len(songs), len(mb_tracks), c.w)
    failmsg = "Found no album tracks for %s%s%s" % (c.y, title, c.w)

    paginatesongs(songs, msg=msg, failmsg=failmsg)


@commands.command(r'encoders?')
def show_encs():
    """ Display available encoding presets. """
    out = "%sEncoding profiles:%s\n\n" % (c.ul, c.w)

    for x, e in enumerate(g.encoders):
        sel = " (%sselected%s)" % (c.y, c.w) if Config.ENCODER.get == x else ""
        out += "%2d. %s%s\n" % (x, e['name'], sel)

    g.content = out
    message = "Enter %sset encoder <num>%s to select an encoder"
    g.message = message % (c.g, c.w)


def matchfunction(func, regex, userinput):
    """ Match userinput against regex.

    Call func, return True if matches.

    """
    # Not supported in python 3.3 or lower
    # match = regex.fullmatch(userinput)
    # if match:
    match = regex.match(userinput)
    if match and match.group(0) == userinput:
        matches = match.groups()
        dbg("input: %s", userinput)
        dbg("function call: %s", func.__name__)
        dbg("regx matches: %s", matches)

        try:
            func(*matches)

        except IndexError:
            if g.debug_mode:
                g.content = ''.join(traceback.format_exception(
                    *sys.exc_info()))
            g.message = F('invalid range')
            g.content = g.content or generate_songlist_display()

        except (ValueError, IOError) as e:
            if g.debug_mode:
                g.content = ''.join(traceback.format_exception(
                    *sys.exc_info()))
            g.message = F('cant get track') % str(e)
            g.content = g.content or\
                generate_songlist_display(zeromsg=g.message)

        except GdataError as e:
            if g.debug_mode:
                g.content = ''.join(traceback.format_exception(
                    *sys.exc_info()))
            g.message = F('no data') % e
            g.content = g.content

        return True


def main():
    """ Main control loop. """
    set_window_title("mpsyt")

    if not g.command_line:
        g.content = logo(col=c.g, version=__version__) + "\n\n"
        g.message = "Enter /search-term to search or [h]elp"
        screen.update()

    # open playlists from file
    playlists.load()

    #open history from file
    history.load()

    arg_inp = ' '.join(g.argument_commands)

    prompt = "> "
    arg_inp = arg_inp.replace(r",,", "[mpsyt-comma]")
    arg_inp = arg_inp.split(",")

    while True:
        next_inp = ""

        if len(arg_inp):
            next_inp = arg_inp.pop(0).strip()
            next_inp = next_inp.replace("[mpsyt-comma]", ",")

        try:
            userinput = next_inp or input(prompt).strip()

        except (KeyboardInterrupt, EOFError):
            userinput = prompt_for_exit()

        for i in g.commands:
            if matchfunction(i.function, i.regex, userinput):
                break

        else:
            g.content = g.content or generate_songlist_display()

            if g.command_line:
                g.content = ""

            if userinput and not g.command_line:
                g.message = c.b + "Bad syntax. Enter h for help" + c.w

            elif userinput and g.command_line:
                sys.exit("Bad syntax")

        screen.update()
