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
import random
import time
import re
import sqlite3

def regexp(expr, item):
    reg = re.compile(expr)
    return reg.search(item) is not None

class SQLiteWhatisDB(object):
    def __init__(self, filename):
        self.dbs = ircutils.IrcDict()
        self.filename = filename

    def close(self):
        for db in self.dbs.itervalues():
            db.commit()
            db.close()

    def _getDb(self, channel):
        if channel not in self.dbs:
            filename = plugins.makeChannelFilename(self.filename, channel)
            self.dbs[channel] = sqlite3.connect(filename)
            c = self.dbs[channel].execute("PRAGMA user_version");
            version = c.fetchone()[0]
            self._upgradeDb(self.dbs[channel], version)
            self.dbs[channel].create_function("REGEXP", 2, regexp)
        return self.dbs[channel]

    def _upgradeDb(self, db, current):
        if (current == 0):
            current=1
            db.execute("CREATE TABLE Reactions (pattern TEXT KEY, reaction TEXT KEY, person TEXT, frequency REAL)")
            db.execute("CREATE UNIQUE INDEX reactionPair ON Reactions (pattern, reaction)")
        db.execute("PRAGMA user_version=%i"%current)
        db.commit()

    def getReaction(self, channel, text):
        c = self._getDb(channel).cursor()
        c.execute("SELECT reaction, pattern, person, frequency FROM Reactions WHERE pattern REGEXP ? ORDER BY RANDOM() * frequency LIMIT 1", (text,))
        res = c.fetchone()
        if res:
            return (res[0], res[1], res[2], res[3])
        return None

    def addReaction(self, channel, pattern, reaction, person=None, frequency=1):
        if person is None:
            person = "my own instincts"
        c = self._getDb(channel).cursor()
        c.execute("SELECT reaction, pattern, person FROM Reactions WHERE pattern = ? AND reaction = ?", (pattern, reaction))
        res = c.fetchone()
        if res == None:
            c.execute("INSERT INTO Reactions (pattern, reaction, person, frequency) VALUES (?, ?, ?, ?)", (pattern, reaction, person, frequency))
            self._getDb(channel).commit()
        else:
            return [res[0], res[1], res[2]]

    def forgetReaction(self, channel, pattern):
        c = self._getDb(channel).cursor()
        res = c.execute("DELETE FROM Reactions WHERE pattern = ?",
                (pattern,))
        self._getDb(channel).commit()
        return bool(res.rowcount > 0)

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

    def addReaction(self, channel, match, reply):
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

WhatisDB = plugins.DB('Whatis', {'sqlite': SQLiteWhatisDB})

class Whatis(callbacks.PluginRegexp):
    """Add the help for "@plugin help Whatis" here
    This should describe *how* to use this plugin."""
    addressedRegexps = ['doRemember']
    unaddressedRegexps = ['doRemember']
    threaded = False

    def __init__(self, irc):
        self.__parent = super(Whatis, self)
        self.__parent.__init__(irc)
        self.db = WhatisDB()
        self.explanations = ircutils.IrcDict()

    def die(self):
        self.__parent.die()
        self.db.close()

    def explain(self, irc, msg, args, channel, text):
        """[<channel>] [<text>]

        Returns the definition for <text> from the database for <channel>. If
        text is not given, it returns the definition for the last reply.
        """
        if (not text):
            if (channel in self.explanations.keys()):
                explanation = self.explanations[channel]
                reaction = explanation[0]
                pattern = explanation[1]
                nick = explanation[2]
                freq = explanation[3]
                irc.reply("%s taught me that '%s' was '%s' %s%% of the time" %
                        (nick, pattern, reaction, freq*100))
            else:
                irc.reply("I haven't said anything yet.")
        else:
            irc.reply("'%s' is '%s'" % (text, self.db.getReply(channel, text)))
    explain = wrap(explain, ['channeldb', optional('text')])

    def forget(self, irc, msg, args, channel, text):
        """[<channel>] <pattern> is <reaction>

        Forgets responding to <pattern> with <reaction> in <channel>
        """
        if self.db.forgetReaction(channel, text):
            irc.replySuccess()
        else:
            irc.reply("I don't remember anything about that.")

    forget = wrap(forget, ['channeldb', 'text'])

    def doRemember(self, irc, msg, match):
        r'(.+)\s+is\s+(.+)'
        text = callbacks.addressed(irc.nick, msg)
        if not text:
            return
        addressed = True
        (key, value) = match.groups()
        self.log.debug("Learning that '%s' means '%s'!", key, value)
        channel = plugins.getChannel(msg.args[0])
        prev = self.db.addReaction(channel, key, value, irc.nick)
        msg.tag("repliedTo")
        if (prev and prev[0] == value):
            irc.reply("I already knew that.")
        elif (prev):
            irc.reply("forgot that %s told me %s meant %s"%(prev[2], prev[1], prev[0]), action=True)
        else:
            irc.replySuccess()

    def doPrivmsg(self, irc, msg):
        if (irc.isChannel(msg.args[0])):
            channel = plugins.getChannel(msg.args[0])
            if (self.registryValue('autoreply', channel) and not msg.tagged("repliedTo")):
                self._reply(channel, irc, msg, False)

    def _reply(self, channel, irc, msg, direct):
        reaction = self.db.getReaction(channel, ' '.join(msg.args[1:]))
        if (reaction):
            self.explanations[channel] = reaction
            reply = (reaction[1], reaction[0].replace("$nick", msg.nick))
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
