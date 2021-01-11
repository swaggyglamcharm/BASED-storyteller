# Set up bot config

from .cfg import cfg, versionInfo
import os

for varname in cfg.paths:
    cfg.paths[varname] = os.path.normpath(cfg.paths[varname])
    if not os.path.isdir(os.path.dirname(cfg.paths[varname])):
        os.makedirs(os.path.dirname(cfg.paths[varname]))


class ConfigProxy:
    def __init__(self, attrs):
        self.attrNames = attrs.keys()
        for varname, varvalue in attrs.items():
            setattr(self, varname, varvalue)

cfg.defaultEmojis = ConfigProxy(cfg.defaultEmojis)
cfg.timeouts = ConfigProxy(cfg.timeouts)
cfg.paths = ConfigProxy(cfg.paths)


# Discord Imports

import discord
from discord.ext.commands import Bot as ClientBaseClass
 

# Util imports

from datetime import datetime
import os
import traceback
import asyncio
import signal
import aiohttp


# BASED Imports

from . import lib, botState, logging
from .databases import guildDB, reactionMenuDB, userDB
from .scheduling import TimedTaskHeap, TimedTask


async def checkForUpdates():
    """Check if any new BASED versions are available, and print a message to console if one is found.
    """
    try:
        BASED_versionCheck = await versionInfo.checkForUpdates(botState.httpClient)
    except versionInfo.UpdatesCheckFailed:
        print("⚠ BASED updates check failed. Either the GitHub API is down, or your BASED updates checker version is depracated: " + versionInfo.BASED_REPO_URL)
    else:
        if BASED_versionCheck.updatesChecked and not BASED_versionCheck.upToDate:
            print("⚠ New BASED update " + BASED_versionCheck.latestVersion + " now available! See " + versionInfo.BASED_REPO_URL + " for instructions on how to update your BASED fork.")


class GracefulKiller:
  kill_now = False
  def __init__(self):
    signal.signal(signal.SIGINT, self.exit_gracefully)
    signal.signal(signal.SIGTERM, self.exit_gracefully)

  def exit_gracefully(self,signum, frame):
    self.kill_now = True


class BasedClient(ClientBaseClass):
    """A minor extension to discord.ext.commands.Bot to include database saving and extended shutdown procedures.

    A command_prefix is assigned to this bot, but no commands are registered to it, so this is effectively meaningless.
    I chose to assign a zero-width character, as this is unlikely to ever be chosen as the bot's actual command prefix, minimising erroneous commands.Bot command recognition. 
    
    :var bot_loggedIn: Tracks whether or not the bot is currently logged in
    :type bot_loggedIn: bool
    """
    def __init__(self, storeUsers=True, storeGuilds=True, storeMenus=True):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="‎", intents=intents)
        self.loggedIn = False
        self.storeUsers = storeUsers
        self.storeGuilds = storeGuilds
        self.storeMenus = storeMenus
        self.storeNone = not(storeUsers or storeGuilds or storeMenus)
        self.launchTime = datetime.utcnow()
        self.killer = GracefulKiller()

    
    def saveAllDBs(self):
        """Save all of the bot's savedata to file.
        This currently saves:
        - the users database
        - the guilds database
        - the reaction menus database
        """
        if self.storeUsers:
            lib.jsonHandler.saveDB(cfg.paths.usersDB, botState.usersDB)
        if self.storeGuilds:
            lib.jsonHandler.saveDB(cfg.paths.guildsDB, botState.guildsDB)
        if self.storeMenus:
            lib.jsonHandler.saveDB(cfg.paths.reactionMenusDB, botState.reactionMenusDB)
        botState.logger.save()
        if not self.storeNone:
            print(datetime.now().strftime("%H:%M:%S: Data saved!"))


    async def shutdown(self):
        """Cleanly prepare for, and then perform, shutdown of the bot.

        This currently:
        - expires all non-saveable reaction menus
        - logs out of discord
        - saves all savedata to file
        """
        if self.storeMenus:
            menus = list(botState.reactionMenusDB.values())
            for menu in menus:
                if not menu.saveable:
                    await menu.delete()
        self.loggedIn = False
        await self.logout()
        self.saveAllDBs()
        print(datetime.now().strftime("%H:%M:%S: Shutdown complete."))
        await botState.httpClient.close()



####### GLOBAL VARIABLES #######

botState.logger = logging.Logger()

# interface into the discord servers
botState.client = BasedClient(storeUsers=True,
                                storeGuilds=True,
                                storeMenus=True)

# commands DB
from . import commands
botCommands = commands.loadCommands()


####### DATABASE FUNCTIONS #####

def loadUsersDB(filePath : str) -> userDB.UserDB:
    """Build a UserDB from the specified JSON file.

    :param str filePath: path to the JSON file to load. Theoretically, this can be absolute or relative.
    :return: a UserDB as described by the dictionary-serialized representation stored in the file located in filePath.
    """
    if os.path.isfile(filePath):
        return userDB.UserDB.fromDict(lib.jsonHandler.readJSON(filePath))
    return userDB.UserDB()


def loadGuildsDB(filePath : str, dbReload : bool = False) -> guildDB.GuildDB:
    """Build a GuildDB from the specified JSON file.

    :param str filePath: path to the JSON file to load. Theoretically, this can be absolute or relative.
    :return: a GuildDB as described by the dictionary-serialized representation stored in the file located in filePath.
    """
    if os.path.isfile(filePath):
        return guildDB.GuildDB.fromDict(lib.jsonHandler.readJSON(filePath))
    return guildDB.GuildDB()


async def loadReactionMenusDB(filePath : str) -> reactionMenuDB.ReactionMenuDB:
    """Build a reactionMenuDB from the specified JSON file.
    This method must be called asynchronously, to allow awaiting of discord message fetching functions.

    :param str filePath: path to the JSON file to load. Theoretically, this can be absolute or relative.
    :return: a reactionMenuDB as described by the dictionary-serialized representation stored in the file located in filePath.
    """
    if os.path.isfile(filePath):
        return await reactionMenuDB.fromDict(lib.jsonHandler.readJSON(filePath))
    return reactionMenuDB.ReactionMenuDB()


####### SYSTEM COMMANDS #######

async def err_nodm(message : discord.Message, args : str, isDM : bool):
    """Send an error message when a command is requested that cannot function outside of a guild

    :param discord.Message message: the discord message calling the command
    :param str args: ignored
    :param bool isDM: ignored
    """
    await message.channel.send("This command can only be used from inside of a server.")


####### MAIN FUNCTIONS #######



@botState.client.event
async def on_guild_join(guild : discord.Guild):
    """Create a database entry for new guilds when one is joined.
    TODO: Once deprecation databases are implemented, if guilds now store important information consider searching for them in deprecated

    :param discord.Guild guild: the guild just joined.
    """
    if botState.client.storeGuilds:
        guildExists = True
        if not botState.guildsDB.idExists(guild.id):
            guildExists = False
            botState.guildsDB.addID(guild.id)
        botState.logger.log("Main", "guild_join", "I joined a new guild! " + guild.name + "#" + str(guild.id) + ("\n -- The guild was added to botState.guildsDB" if not guildExists else ""),
                    category="guildsDB", eventType="NW_GLD")


@botState.client.event
async def on_guild_remove(guild : discord.Guild):
    """Remove the database entry for any guilds the bot leaves.
    TODO: Once deprecation databases are implemented, if guilds now store important information consider moving them to deprecated.

    :param discord.Guild guild: the guild just left.
    """
    if botState.client.storeGuilds:
        guildExists = False
        if botState.guildsDB.idExists(guild.id):
            guildExists = True
            botState.guildsDB.removeID(guild.id)
        botState.logger.log("Main", "guild_remove", "I left a guild! " + guild.name + "#" + str(guild.id) + ("\n -- The guild was removed from botState.guildsDB" if guildExists else ""),
                    category="guildsDB", eventType="NW_GLD")


@botState.client.event
async def on_ready():
    """Bot initialisation (called on bot login) and behaviour loops.
    Currently includes:
    - regular database saving to JSON

    TODO: Implement dynamic timedtask checking period
    """
    botState.httpClient = aiohttp.ClientSession()

    ##### EMOJI INITIALIZATION #####
    emojiVars = []
    emojiListVars = []

    for varname in cfg.defaultEmojis.attrNames:
        varvalue = getattr(cfg.defaultEmojis, varname)
        if type(varvalue) == lib.emojis.UninitializedBasedEmoji:
            emojiVars.append(varname)
            continue
        elif type(varvalue) == list:
            onlyEmojis = True
            for item in varvalue:
                if type(item) != lib.emojis.UninitializedBasedEmoji:
                    onlyEmojis = False
                    break
            if onlyEmojis:
                emojiListVars.append(varname)
                continue
        raise ValueError("Invalid config variable in cfg.defaultEmojis: Emoji config variables must be either UninitializedBasedEmoji or List[UninitializedBasedEmoji]")
    
    for varname in emojiVars:
        setattr(cfg.defaultEmojis, varname, lib.emojis.BasedEmoji.fromUninitialized(getattr(cfg.defaultEmojis, varname)))
    
    for varname in emojiListVars:
        working = []
        for item in getattr(cfg.defaultEmojis, varname):
            working.append(lib.emojis.BasedEmoji.fromUninitialized(item))
            
        setattr(cfg.defaultEmojis, varname, working)
    
    # Ensure all emojis have been initialized
    for varName, varValue in vars(cfg).items():
        if isinstance(varValue, lib.emojis.UninitializedBasedEmoji):
            raise RuntimeError("Uninitialized emoji still remains in cfg after emoji initialization: '" + varName + "'")

    botState.usersDB = loadUsersDB(cfg.paths.usersDB)
    botState.guildsDB = loadGuildsDB(cfg.paths.guildsDB)

    # Set help embed thumbnails
    for levelSection in botCommands.helpSectionEmbeds:
        for helpSection in levelSection.values():
            for embed in helpSection:
                embed.set_thumbnail(url=botState.client.user.avatar_url_as(size=64))

    botState.reactionMenusTTDB = TimedTaskHeap.TimedTaskHeap()
    if not os.path.exists(cfg.paths.reactionMenusDB):
        try:
            f = open(cfg.paths.reactionMenusDB, 'x')
            f.write("{}")
            f.close()
        except IOError as e:
            botState.logger.log("main","on_ready","IOError creating reactionMenuDB save file: " + e.__class__.__name__, trace=traceback.format_exc())

    botState.reactionMenusDB = await loadReactionMenusDB(cfg.paths.reactionMenusDB)

    botState.dbSaveTT = TimedTask.TimedTask(expiryDelta=lib.timeUtil.timeDeltaFromDict(cfg.timeouts.dataSaveFrequency), autoReschedule=True, expiryFunction=botState.client.saveAllDBs)
    botState.updatesCheckTT = TimedTask.TimedTask(expiryDelta=lib.timeUtil.timeDeltaFromDict(cfg.timeouts.BASED_updateCheckFrequency), autoReschedule=True, expiryFunction=checkForUpdates)

    print("BASED " + versionInfo.BASED_VERSION + " loaded.\nClient logged in as {0.user}".format(botState.client))
    await checkForUpdates()

    await botState.client.change_presence(activity=discord.Game("BASED APP"))
    # bot is now logged in
    botState.client.loggedIn = True

    # execute regular tasks while the bot is logged in
    while botState.client.loggedIn:
        if cfg.timedTaskCheckingType == "fixed":
            await asyncio.sleep(cfg.timedTaskLatenessThresholdSeconds)
        # elif cfg.timedTaskCheckingType == "dynamic":

        await botState.dbSaveTT.doExpiryCheck()
        await botState.reactionMenusTTDB.doTaskChecking()
        await botState.updatesCheckTT.doExpiryCheck()

        if botState.client.killer.kill_now:
            botState.shutdown = botState.ShutDownState.shutdown
            print("shutdown signal received, shutting down...")
            await botState.client.shutdown()


@botState.client.event
async def on_message(message : discord.Message):
    """Called every time a message is sent in a server that the bot has joined
    Currently handles:
    - command calling

    :param discord.Message message: The message that triggered this command on sending
    """
    # ignore messages sent by bots
    if message.author.bot:
        return

    # Check whether the command was requested in DMs
    isDM = message.channel.type in [discord.ChannelType.private, discord.ChannelType.group]

    if isDM:
        commandPrefix = cfg.defaultCommandPrefix
    else:
        commandPrefix = botState.guildsDB.getGuild(message.guild.id).commandPrefix

    # For any messages beginning with commandPrefix
    if message.content.startswith(commandPrefix) and len(message.content) > len(commandPrefix):
        # replace special apostraphe characters with the universal '
        msgContent = message.content.replace("‘", "'").replace("’", "'")

        # split the message into command and arguments
        if len(msgContent[len(commandPrefix):]) > 0:
            command = msgContent[len(commandPrefix):].split(" ")[0]
            args = msgContent[len(commandPrefix) + len(command) + 1:]

        # if no command is given, ignore the message
        else:
            return

        # infer the message author's permissions
        if message.author.id in cfg.developers:
            accessLevel = 3
        elif message.author.permissions_in(message.channel).administrator:
            accessLevel = 2
        else:
            accessLevel = 0

        try:
            # Call the requested command
            commandFound = await botCommands.call(command, message, args, accessLevel, isDM=isDM)
        
        # If a non-DMable command was called from DMs, send an error message 
        except lib.exceptions.IncorrectCommandCallContext:
            await err_nodm(message, "", isDM)
            return

        # If the command threw an exception, print a user friendly error and log the exception as misc.
        except Exception as e:
            await message.channel.send("An unexpected error occured when calling this command. The error has been logged.\nThis command probably won't work until we've looked into it.")
            botState.logger.log("Main", "on_message", "An unexpected error occured when calling command '" +
                            command + "' with args '" + args + "': " + type(e).__name__, trace=traceback.format_exc())
            print(traceback.format_exc())
            commandFound = True

        # Command not found, send an error message.
        if not commandFound:
            # await message.channel.send(":question: Unknown command. Type `" + commandPrefix + "help` for a list of commands.")
            try:
                await message.add_reaction(cfg.emojis.unknownCommand.sendable)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden) as e:
                botState.logger.log("main", "on_message", "failed to add reaction for unknown command",
                    eventType = type(e).__name__, trace = traceback.format_exc())

    # Non-command messages
    elif not isDM:
        callingGuild = botState.guildsDB.getGuild(message.guild.id)
        if message.channel.id == callingGuild.storyChannelID:
            if message.author.id == callingGuild.lastAuthorID:
                await message.channel.send(":boom: **Story broken, " + message.author.mention + "!**")
                callingGuild.story = ""
                callingGuild.lastAuthorID = -1

            elif message.content == "" or message.content == "." and callingGuild.story == "":
                pass

            elif " " in message.content:
                firstWord = message.content.split(" ")[0]
                if len(message.content.split(" ")) > 2 or not(firstWord == "..." or len(firstWord) == 1 and firstWord in ".,!?"):
                    await message.channel.send(":boom: **Story broken, " + message.author.mention + "!**")
                    callingGuild.story = ""
                    callingGuild.lastAuthorID = -1
                else:
                    callingGuild.story += message.content
                    callingGuild.lastAuthorID = message.author.id

            elif len(callingGuild.story) + len(message.content) + 1 > 2000:
                await message.channel.send(":boom: **Max story length exceeded!**")
            
            elif message.content == ".":
                await message.channel.send("**Story complete!**")
                await message.channel.send(callingGuild.story + ("" if callingGuild.story[-1] in cfg.ignoredSymbols else "."))
                callingGuild.story = ""
                callingGuild.lastAuthorID = -1

            elif message.content[0] in ",!?":
                callingGuild.story += message.content
                callingGuild.lastAuthorID = message.author.id
            
            else:
                callingGuild.story += " " + message.content
                callingGuild.lastAuthorID = message.author.id


@botState.client.event
async def on_raw_reaction_add(payload : discord.RawReactionActionEvent):
    """Called every time a reaction is added to a message.
    If the message is a reaction menu, and the reaction is an option for that menu, trigger the menu option's behaviour.

    :param discord.RawReactionActionEvent payload: An event describing the message and the reaction added
    """
    if payload.user_id != botState.client.user.id:
        _, user, emoji = await lib.discordUtil.reactionFromRaw(payload)
        if None in [user, emoji]:
            return

        if payload.message_id in botState.reactionMenusDB and \
                botState.reactionMenusDB[payload.message_id].hasEmojiRegistered(emoji):
            await botState.reactionMenusDB[payload.message_id].reactionAdded(emoji, user)


@botState.client.event
async def on_raw_reaction_remove(payload : discord.RawReactionActionEvent):
    """Called every time a reaction is removed from a message.
    If the message is a reaction menu, and the reaction is an option for that menu, trigger the menu option's behaviour.

    :param discord.RawReactionActionEvent payload: An event describing the message and the reaction removed
    """
    if payload.user_id != botState.client.user.id:
        _, user, emoji = await lib.discordUtil.reactionFromRaw(payload)
        if None in [user, emoji]:
            return

        if payload.message_id in botState.reactionMenusDB and \
                botState.reactionMenusDB[payload.message_id].hasEmojiRegistered(emoji):
            await botState.reactionMenusDB[payload.message_id].reactionRemoved(emoji, user)


@botState.client.event
async def on_raw_message_delete(payload : discord.RawMessageDeleteEvent):
    """Called every time a message is deleted.
    If the message was a reaction menu, deactivate and unschedule the menu.

    :param discord.RawMessageDeleteEvent payload: An event describing the message deleted.
    """
    if payload.message_id in botState.reactionMenusDB:
        await botState.reactionMenusDB[payload.message_id].delete()


@botState.client.event
async def on_raw_bulk_message_delete(payload : discord.RawBulkMessageDeleteEvent):
    """Called every time a group of messages is deleted.
    If any of the messages were a reaction menus, deactivate and unschedule those menus.

    :param discord.RawBulkMessageDeleteEvent payload: An event describing all messages deleted.
    """
    for msgID in payload.message_ids:
        if msgID in botState.reactionMenusDB:
            await botState.reactionMenusDB[msgID].delete()


def run():
    if not (bool(cfg.botToken) ^ bool(cfg.botToken_envVarName)):
        raise ValueError("You must give exactly one of either cfg.botToken or cfg.botToken_envVarName")

    if cfg.botToken_envVarName and cfg.botToken_envVarName not in os.environ:
        raise KeyError("Bot token environment variable " + cfg.botToken_envVarName + " not set (cfg.botToken_envVarName")

    # Launch the bot!! 🤘🚀
    botState.client.run(cfg.botToken if cfg.botToken else os.environ[cfg.botToken_envVarName])
    return botState.shutdown
    
