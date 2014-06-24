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

    def getReactions(self, channel, pattern):
        c = self._getDb(channel).cursor()
        c.execute("SELECT reaction, pattern, person, frequency FROM Reactions WHERE pattern = ? ORDER BY reaction", (pattern,))

        ret = []

        for reaction in c:
            ret.append({
                'reaction': reaction[0],
                'pattern': reaction[1],
                'person': reaction[2],
                'frequency': reaction[3]
            })
        return ret

    def produceReaction(self, channel, text):
        c = self._getDb(channel).cursor()
        c.execute("SELECT reaction, pattern, person, frequency FROM Reactions WHERE REGEXP(pattern, ?) ORDER BY RANDOM() * frequency LIMIT 1", (text,))
        res = c.fetchone()
        if res:
            return {
                'reaction': res[0],
                'pattern': res[1],
                'person': res[2],
                'frequency': res[3]
            }
        return None

    def addReaction(self, channel, pattern, reaction, person=None, frequency=1):
        if person is None:
            person = "instinct"
        c = self._getDb(channel).cursor()
        try:
            c.execute("INSERT OR ABORT INTO Reactions (pattern, reaction, person, frequency) VALUES (?, ?, ?, ?)", (pattern, reaction, person, frequency))
            return True
        except:
            return False


    def forgetReaction(self, channel, pattern, reaction):
        c = self._getDb(channel).cursor()
        res = c.execute("DELETE FROM Reactions WHERE pattern = ? AND reaction = ?",
                (pattern,reaction))
        self._getDb(channel).commit()
        return bool(res.rowcount > 0)

WhatisDB = plugins.DB('Whatis', {'sqlite': SQLiteWhatisDB})

class Whatis(callbacks.PluginRegexp):
    """Add the help for "@plugin help Whatis" here
    This should describe *how* to use this plugin."""
    addressedRegexps = ['doRemember']
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
                irc.reply("%(person)s taught me that '%(pattern)s' was '%(reaction)s' %(frequency)f% of the time" % reaction)
            else:
                irc.reply("I haven't said anything yet.")
        else:
            reactions = self.db.getReactions(channel, text).result()
            if len(reactions) == 0:
                irc.reply("I have no idea what you are talking about.")
            else:
                reactions = map(lambda r: "%(person)s: P('%(reaction)s')=%(frequency).1f"%r, reactions)
                irc.reply(("'%s' is "%(text))+', '.join(reactions))

    explain = wrap(explain, ['channeldb', optional('text')])

    def forget(self, irc, msg, args, channel, text):
        """[<channel>] [that] <text> OR [that] <pattern> is <text>

        Asks me to forget the latest thing I said about <text>, if I can
        remember what it was or you tell me. The 'that' is optional syntactic
        sugar.
        """

        text = re.match("(?:that )?(.+)", text).groups()[0]
        definitionSplit = re.match("(.+)\s+is\s+(.+)", text)
        if definitionSplit:
            pattern, reaction = definitionSplit.groups()
            if self.db.forgetReaction(channel, pattern, reaction).result():
                irc.replySuccess()
                return
        if channel in self.explanations and self.explanations[channel]['pattern'] == text:
            if self.db.forgetReaction(channel, text, self.explanations[channel]['reaction']):
                irc.replySuccess()
                return

        reactions = self.db.getReactions(channel, text).result()

        if len(reactions) == 1:
            if self.db.forgetReaction(channel, text, reactions[0]['reaction']):
                irc.replySuccess()
        elif len(reactions) == 1:
            irc.reply("I don't remember anything about that.")
        else:
            irc.reply("You'll have to be more specific about what I'm forgetting.")

    forget = wrap(forget, ['channeldb', 'text'])

    def doRemember(self, irc, msg, match):
        r'(.+)\s+is\s+(.+)'
        if not callbacks.addressed(irc.nick, msg):
            return

        (pattern, reaction) = match.groups()
        self.log.info("Learning that '%s' means '%s'!", pattern, reaction)
        channel = plugins.getChannel(msg.args[0])
        msg.tag("repliedTo")

        if self.db.addReaction(channel, pattern, reaction).result():
            existing = self.db.getReactions(channel, pattern).result()
            if len(existing) > 1:
                irc.reply("I now have %d meanings for %s."%(len(existing), pattern))
            else:
                irc.replySuccess()
        else:
            irc.reply("I already knew that.")

    def doPrivmsg(self, irc, msg):
        if (irc.isChannel(msg.args[0])):

            channel = plugins.getChannel(msg.args[0])

            if (not msg.tagged("repliedTo")):
                self._reply(channel, irc, msg, False)

    @staticmethod
    def extractTag(text):
        matches = re.match("(<.+>)?(.+)", text)
        if matches:
            tag, text = matches.groups()
            return (tag[1:-1], text)
        return (None, text)

    def _reply(self, channel, irc, msg, direct):
        reaction = self.db.produceReaction(channel, ' '.join(msg.args[1:])).result()
        self.log.info("Got reaction for %r: %r", ' '.join(msg.args[1:]), reaction)

        if (reaction):

            self.explanations[channel] = reaction
            tag, text = self.extractTag(reaction['reaction'])

            text = text.replace('$nick', msg.nick)

            if tag == 'action':
                irc.reply(text, action=True)
            elif tag == 'reply':
                irc.reply(text, prefixNick=direct)
            elif tag == 'markov':
                callbacks.NestedCommandsIrcProxy(irc, msg,
                        ["say", ['markov', text]])
            else:
                irc.reply("%(pattern)s is %(reaction)s" % reaction, prefixNick=direct)
            return True
        else:
            return False

Class = Whatis 

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
