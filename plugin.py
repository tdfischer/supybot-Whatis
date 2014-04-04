###
# Copyright (c) 2009-2014, Torrie Fischer
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

###
import Queue

import supybot.utils as utils
import supybot.world as world
from supybot.commands import *
import supybot.irclib as irclib
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.schedule as schedule
import supybot.callbacks as callbacks
import anydbm
import threading
import random
import time
import re

class DbmWhatisDB(object):
    def __init__(self, filename):
        self.dbs = ircutils.IrcDict()
        self.filename = filename
    def close(self):
        for db in self.dbs.values():
            db.close()

    def _getDb(self, channel):
        if channel not in self.dbs:
            filename = plugins.makeChannelFilename(self.filename, channel)
            self.dbs[channel] = anydbm.open(filename, 'c')
        return self.dbs[channel]
    
    def _flush(self, db):
        if hasattr(db, 'sync'):
            db.sync()
        if hasattr(db, 'flush'):
            db.flush()

    def keys(self, channel):
        return self._getDb(channel).keys()

    def addReply(self, channel, match, reply):
        ret = None
        db = self._getDb(channel)
        if (match in db):
            ret = db[match]
            db[match] = '|'.join(db[match].split('|')+reply)
        else:
            db[match] = reply

    def setReply(self, channel, match, reply):
        ret = None
        db = self._getDb(channel)
        if (match in db):
            ret = db[match]
        db[match] = reply
        return ret

    def hasReply(self, channel, match):
        return match in self._getDb(channel)

    def getReply(self, channel, match):
        db = self._getDb(channel)
        if (match in db):
            return db[match]
        return None

    def getMatches(self, channel, needle):
        db = self._getDb(channel)
        matches = []
        for pattern in db.keys():
            try:
                if (re.match(pattern, needle)):
                    matches.append((pattern, db[pattern]))
            except:
                if (needle.find(pattern) > -1):
                    matches.append((pattern, db[pattern]))
        return matches

    def searchPatterns(self, channel, needle):
        db = self._getDb(channel)
        matches = []
        for pattern in db.keys():
            try:
                if (pattern == needle):
                    matches.append((pattern, db[pattern]))
                elif (re.match(needle, pattern)):
                    matches.append((pattern, db[pattern]))
            except:
                if (needle.find(pattern) > -1):
                    matches.append((pattern, db[pattern]))
        return matches

    def forgetPattern(self, channel, pattern):
        db = self._getDb(channel)
        if (pattern in db):
            del db[pattern]
            return True
        return False

    def generateReply(self, channel, text):
        matches = self.getMatches(channel, text)
        if (len(matches)>=1):
            return random.choice(matches)
        else:
            return None

WhatisDB = plugins.DB('Whatis', {'anydbm': DbmWhatisDB})

class WhatisWorkQueue(threading.Thread):
    def __init__(self, log, *args, **kwargs):
        name = 'Thread #%s (WhatisWorkQueue)' % world.threadsSpawned
        self.log = log
        world.threadsSpawned += 1
        threading.Thread.__init__(self, name=name)
        self.db = WhatisDB(*args, **kwargs)
        self.q = Queue.Queue()
        self.killed = False
        self.setDaemon(True)
        self.start()

    def die(self):
        self.killed = True
        self.q.put(None)

    def enqueue(self, f):
        self.q.put(f)

    def run(self):
        while not self.killed:
            f = self.q.get()
            if f is not None:
                f(self.db)
        self.db.close()

class Whatis(callbacks.PluginRegexp):
    """Add the help for "@plugin help Whatis" here
    This should describe *how* to use this plugin."""
    addressedRegexps = ['doRemember', 'doExplain']
    unaddressedRegexps = ['doRemember']
    def __init__(self, irc):
        self.__parent = super(Whatis, self)
        self.__parent.__init__(irc)
        self.db = WhatisDB()
        self.explanations = ircutils.IrcDict()

    def die(self):
        self.__parent.die()
        self.db.close()

    def count(self, irc, msg, args, channel):
        """[<channel>]

        Returns the number of patterns in the database for <channel>
        """
        irc.reply(len(self.db.keys(channel)))
    count = wrap(count, ['channeldb'])

    def explain(self, irc, msg, args, channel, text):
        """[<channel>] [<text>]

        Returns the definition for <text> from the database for <channel>. If
        text is not given, it returns the definition for the last reply.
        """
        if (not text):
            if (channel in self.explanations.keys()):
                irc.reply("'%s' was '%s'" % self.explanations[channel])
            else:
                irc.reply("I haven't said anything yet.")
        else:
            irc.reply("'%s' is '%s'" % (text, self.db.getReply(channel, text)))
    explain = wrap(explain, ['channeldb', optional('text')])

    def forget(self, irc, msg, args, channel, text):
        """[<channel>] <text>

        Forgets what <text> means in <channel>
        """
        if (self.db.forgetPattern(channel, text)):
            irc.replySuccess()
        else:
            irc.reply("I couldn't find any entries for '%s'"%text)
    forget = wrap(forget, ['channeldb', 'text'])

    def match(self, irc, msg, args, channel, text):
        """[<channel>] <text>

        Searches the database for matches against <text>
        """
        matches = self.db.getMatches(channel, text)
        if (len(matches) == 0):
            irc.reply("Nothing found.")
        else:
            irc.reply("%i matches: %s"%(len(matches), ', '.join(map(lambda x:x[0], matches))))
    match = wrap(match, ['channeldb', 'text'])

    def find(self, irc, msg, args, channel, text):
        """[<channel>] <text>

        Searches the database for patterns that match <text>
        """
        matches = self.db.searchPatterns(channel, text)
        if (len(matches) == 0):
            irc.reply("Nothing found.")
        else:
            irc.reply("%i matches: %s"%(len(matches), ',' .join(map(lambda x:x[0], matches))))
    find = wrap(find, ['channeldb', 'text'])

    def doExplain(self, irc, msg, match):
        r'(.+)\?'
        term = match.group(0)[0:-1]
        channel = plugins.getChannel(msg.args[0])
        if (callbacks.addressed(irc.nick, msg)):
            msg.tag("repliedTo")
            reply = self._reply(channel, irc, term, False)
            if (reply is False):
                irc.reply("I dunno.")

    def doRemember(self, irc, msg, match):
        r'(.+)\s+is\s+(.+)'
        addressed = callbacks.addressed(irc.nick, msg)
        if (not addressed and not self.registryValue('autolearn')):
            self.log.debug("Not learning because autolearn is false and we're not addressed")
            return
        if (addressed):
            text = addressed
            addressed = True
        else:
            text = msg.args[1]
            addressed = False
        (key, value) = match.groups()
        if (value.startswith('also')):
            value = ' '.join(value.split()[1:])
            also = True
        else:
            also = False
        if (addressed or (not addressed and not self.db.hasReply(key))):
            self.log.debug("Learning that '%s' means '%s'!", key, value)
            channel = plugins.getChannel(msg.args[0])
            if (also):
                prev = self.db.addReply(channel, key, value)
            else:
                prev = self.db.setReply(channel, key, value)
            msg.tag("repliedTo")
            if (addressed):
                if (also):
                    irc.replySuccess()
                else:
                    if (prev == value):
                        irc.reply("I already knew that.")
                    elif (prev):
                        irc.reply("forgot that %s meant %s"%(key, prev), action=True)
                    else:
                        irc.replySuccess()

    def doPrivmsg(self, irc, msg):
        if (irc.isChannel(msg.args[0])):
            channel = plugins.getChannel(msg.args[0])
            if (self.registryValue('autoreply', channel) and not msg.tagged("repliedTo")):
                self._reply(channel, irc, msg, False)

    def _reply(self, channel, irc, msg, direct):
        reply = self.db.generateReply(channel, ' '.join(msg.args[1:]))
        if (reply):
            self.explanations[channel] = reply
            reply = (reply[0], reply[1].replace("$nick", msg.nick))
            if (reply[1].startswith("<action>")):
                irc.reply(reply[1].replace("<action>", '', 1), action=True)
            elif (reply[1].startswith("<reply>")):
                irc.reply(reply[1].replace("<reply>", '', 1), prefixNick=direct)
            else:
                irc.reply("%s is %s" % (reply[0], reply[1]), prefixNick=direct)
            return True
        else:
            return False



Class = Whatis 


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
