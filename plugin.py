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

import functools
import Queue
import threading
import logging

class Promise(object):
  def __init__(self):
    self.event = threading.Event()
    self.value = None
    self.exception = None
  
  def result(self):
    logging.debug("Waiting to resolve promise")
    self.event.wait()
    logging.debug("Promise finished!")
    if self.exception is not None:
        raise self.exception
    return self.value

  def finish(self, val):
    self.value = val
    self.event.set()

  def errored(self, exc):
    self.exception = exc
    self.event.set()

class ThreadProtectionFacade(object):
  def __init__(self, wrappedClass, *args, **kwargs):

    def f(*args, **kwargs):
      return wrappedClass(*args, **kwargs)

    self.__makeWrapped = f
    self.__wrapEvent = threading.Event()

    self.__jobs = Queue.Queue()
    self.__thread = threading.Thread(target=self.__objThread)
    self.__thread.start()

  def __del__(self):
    self._dispose()

  def _dispose(self):
    self.__jobs.put(None)

  def __objThread(self):
    self._wrapped = self.__makeWrapped()
    self.__wrapEvent.set()
    while True:
      job = self.__jobs.get()
      if job is None:
        logging.debug("Quitting job thread")
        return
      func, promise = job
      logging.debug("Processing job %r", func)
      try:
          promise.finish(func())
      except Exception, e:
          promise.errored(e)

  def __schedule(self, func, *args, **kwargs):
    p = Promise()
    logging.debug("Scheduling %r", func)
    self.__jobs.put((functools.partial(func, *args, **kwargs), p))
    return p

  def __getattr__(self, key): 
    self.__wrapEvent.wait()
    val = getattr(self._wrapped, key)
    if threading.currentThread() != self.__thread and callable(val):
      @functools.wraps(val)
      def schedule(*args, **kwargs):
        return self.__schedule(val, *args, **kwargs)
      return schedule
    else:
      return val

def regexp(expr, item):
    reg = re.compile(expr)
    logging.info("Matched %r against %r to get %r", expr, item, reg)
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
            self.dbs[channel].text_factory = str
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
        c.execute("SELECT reaction, pattern, person, frequency FROM Reactions WHERE REGEXP(pattern, ?) ORDER BY RANDOM() * frequency LIMIT 1", (text,))
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

WhatisDB = plugins.DB('Whatis', {'sqlite': SQLiteWhatisDB})

class Whatis(callbacks.PluginRegexp):
    """Add the help for "@plugin help Whatis" here
    This should describe *how* to use this plugin."""
    addressedRegexps = ['doRemember']
    unaddressedRegexps = ['doRemember']
    threaded = False

    def __init__(self, irc):
        self.__jobs = Queue.Queue()
        self.__parent = super(Whatis, self)
        self.__parent.__init__(irc)
        self.db = ThreadProtectionFacade(WhatisDB)
        self.explanations = ircutils.IrcDict()

    def die(self):
        self.__parent.die()
        self.db.close()
        self.db._dispose()

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
            irc.reply("'%s' is '%s'" % (text, self.db.getReply(channel, text).result()))
    explain = wrap(explain, ['channeldb', optional('text')])

    def forget(self, irc, msg, args, channel, text):
        """[<channel>] <pattern> is <reaction>

        Forgets responding to <pattern> with <reaction> in <channel>
        """
        if self.db.forgetReaction(channel, text).result():
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
        self.log.info("Learning that '%s' means '%s'!", key, value)
        channel = plugins.getChannel(msg.args[0])
        msg.tag("repliedTo")
        prev = self.db.addReaction(channel, key, value, 'instinct').result()
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
        reaction = self.db.getReaction(channel, ' '.join(msg.args[1:])).result()
        self.log.info("Got reaction for %r: %r", ' '.join(msg.args[1:]), reaction)
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
