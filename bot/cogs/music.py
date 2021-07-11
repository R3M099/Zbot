import discord
import wavelink
import typing as t
import re
import datetime as dt
import asyncio
import random
from enum import Enum
import aiohttp
from discord.ext import commands

URL_REGEX = r"(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'\".,<>?«»“”‘’]))"

LYRICS_URL = "https://some-random-api.ml/lyrics?title="

HZ_BANDS = (20, 40, 63, 100, 150, 250, 400, 450, 630, 1000, 1600, 2500, 4000, 10000, 16000)

OPTIONS = {
	"1️⃣": 0,
    "2⃣": 1,
    "3⃣": 2,
    "4⃣": 3,
    "5⃣": 4,
}

class AlreadyConnectedToChannel(commands.CommandError):
	pass

class NoVoiceChannel(commands.CommandError):
	pass

class QueueIsEmpty(commands.CommandError):
	pass

class NoTracksFound(commands.CommandError):
	pass

class PlayerIsAlreadyPaused(commands.CommandError):
	pass

class PlayerIsNotPaused(commands.CommandError):
	pass

class NoMoreTracks(commands.CommandError):
	pass

class NoPreviousTracks(commands.CommandError):
	pass

class InvalidRepeatMode(commands.CommandError):
	pass

class VolumeTooLow(commands.CommandError):
	pass

class VolumeTooHigh(commands.CommandError):
	pass

class MaxVolume(commands.CommandError):
	pass

class MinVolume(commands.CommandError):
	pass

class NoLyricsFound(commands.CommandError):
	pass

class InvalidEQPreset(commands.CommandError):
	pass

class NonExistentEQBand(commands.CommandError):
	pass

class EQGainOutOfBounds(commands.CommandError):
	pass


class RepeatMode(Enum):
	NONE = 0
	ONE = 1
	ALL = 2

class Queue:
	def __init__(self):
		self._queue = []
		self.position = 0
		self.repeat_mode = RepeatMode.NONE

	@property
	def is_empty(self):
		return not self._queue

	@property
	def current_track(self):
		if not self._queue:
			raise QueueIsEmpty

		if self.position <= len(self._queue) - 1:
			return self._queue[self.position]

	@property
	def upcoming(self):
		if not self._queue:
			raise QueueIsEmpty
		
		return self._queue[self.position + 1:]

	@property
	def history(self):
		if not self._queue:
			raise QueueIsEmpty
		
		return self._queue[:self.position]

	@property
	def length(self):
		return len(self._queue)

	def add(self, *args):
		self._queue.extend(args)

	def get_next_track(self):
		if not self._queue:
			raise QueueIsEmpty

		self.position += 1

		if self.position < 0:
			return None

		elif self.position > len(self._queue) - 1:
			if self.repeat_mode == RepeatMode.ALL:
				self.postion = 0

			else:
				return None

		return self._queue[self.position]

	def shuffle(self):
		if not self._queue:
			raise QueueIsEmpty

		upcoming = self.upcoming
		random.shuffle(upcoming)
		self._queue = self._queue[:self.position + 1]
		self._queue.extend(upcoming)

	def set_repeat_mode(self, mode):
		if mode == "none":
			self.repeat_mode = RepeatMode.NONE

		elif mode == "1":
			self.repeat_mode = RepeatMode.ONE

		elif mode == "all":
			self.repeat_mode = RepeatMode.ALL

	def empty(self):
		self._queue.clear()

class Player(wavelink.Player):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.queue = Queue()
		self.eq_levels = [0.] * 15

	async def connect(self, ctx, channel = None):
		if self.is_connected:
			raise AlreadyConnectedToChannel

		if (channel := getattr(ctx.author.voice, "channel", channel)) is None:
			raise NoVoiceChannel

		await super().connect(channel.id)
		return channel

	async def teardown(self):
		try:
			await self.destroy()
		except KeyError:
			pass

	async def add_tracks(self, ctx, tracks):
		if not tracks:
			raise NoTracksFound

		if isinstance(tracks, wavelink.TrackPlaylist):
			self.queue.add(*tracks.tracks)

		elif len(tracks) == 1:
			self.queue.add(tracks[0])
			await ctx.send(f"Added **{tracks[0].title}** to the queue.")

		else:
			if (track := await self.choose_track(ctx, tracks)) is not None:
				self.queue.add(track)
				await ctx.send(f"Added **{track.title}** to the queue.")

		if not self.is_playing and not self.queue.is_empty:
			await self.start_playback()

	async def choose_track(self, ctx, tracks):
		def _check(r, u):
			return (
				r.emoji in OPTIONS.keys()
				and u == ctx.author
				and r.message.id == msg.id
			)

		e = discord.Embed(
			title = "Choose a song.",
			description = (
				"\n".join(
					f"**{i+1}.** {t.title} ({t.length//60000}:{str(t.length%60).zfill(2)})"
					for i, t in enumerate(tracks[:5])
				)
			),
			color = ctx.author.color,
			timestamp = dt.datetime.utcnow()
		)
		e.set_author(name = "Query results:")
		e.set_footer(text = f"Requested by {ctx.author.display_name}", icon_url = ctx.author.avatar_url)

		msg = await ctx.send(embed = e)
		for emoji in list(OPTIONS.keys())[:min(len(tracks), len(OPTIONS))]:
			await msg.add_reaction(emoji)

		try:
			reaction, _ =await self.bot.wait_for("reaction_add", timeout = 60.0, check = _check)

		except asyncio.TimeoutError:
			await msg.delete()
			await ctx.message.delete()
		else:
			await msg.delete()
			return tracks[OPTIONS[reaction.emoji]]

	async def start_playback(self):
		await self.play(self.queue.current_track)

	async def advance(self):
		try:
			if (track := self.queue.get_next_track()) is not None:
				await self.play(track)

		except QueueIsEmpty:
			pass

	async def repeat_track(self):
		await self.play(self.queue.current_track)

class Music(commands.Cog, wavelink.WavelinkMixin):
	def __init__(self, bot):
		self.bot = bot
		self.wavelink = wavelink.Client(bot = bot)
		self.bot.loop.create_task(self.start_nodes())

	@commands.Cog.listener()
	async def on_voice_state_update(self, member, before, after):
		if not member.bot and after.channel is None:
			if not [m for m in before.channel.members if not m.bot]:
				await self.get_player(member.guild).teardown()

	@wavelink.WavelinkMixin.listener()
	async def on_node_ready(self, node):
		print(f"Wavelink node `{node.identifier}` ready!")

	@wavelink.WavelinkMixin.listener("on_track_stuck")

	@wavelink.WavelinkMixin.listener("on_track_end")

	@wavelink.WavelinkMixin.listener("on_track_exception")

	async def on_player_stop(self, ctx, payload):
		if payload.player.queue.repeat_mode == RepeatMode.ONE:
			await payload.player.repeat_track()

		else:
			await payload.player.advance()

	async def cog_check(self, ctx):
		if isinstance(ctx.channel, discord.DMChannel):
			await ctx.send("Music commands are not available in DMs.")
			return False

		return True

	async def start_nodes(self):
		await self.bot.wait_until_ready()

		nodes = {
			"MAIN":{
				"host":"127.0.0.1",
				"port":2333,
				"rest_uri":"http://127.0.0.1:2333",
				"password":"youshallnotpass",
				"identifier":"MAIN",
				"region":"europe",
			}
		}

		for node in nodes.values():
			await self.wavelink.initiate_node(**node)

	def get_player(self, obj):
		if isinstance(obj, commands.Context):
			return self.wavelink.get_player(obj.guild.id, cls = Player, context = obj)

		elif isinstance(obj, discord.Guild):
			return self.wavelink.get_player(obj.id, cls = Player)


	#------------------Join Command--------------------

	@commands.command(name = "join", aliases = ["connect"])
	async def connect_command(self, ctx, *, channel:t.Optional[discord.VoiceChannel]):
		player = self.get_player(ctx)
		channel = await player.connect(ctx, channel)
		await ctx.send(f"Joined **{channel.name}**.")

	#------------------Join Command Error Handling--------------------

	@connect_command.error
	async def connect_command_error(self, ctx, exc):
		if isinstance(exc, AlreadyConnectedToChannel):
			await ctx.send("Already connected to a voice channel.")

		elif isinstance(exc, NoVoiceChannel):
			await ctx.send("No suitable voice channel provided.")


	#----------------Leave Command----------------------

	@commands.command(name = "leave", aliases = ["dc", "disconnect"])
	async def disconnect_command(self, ctx):
		player = self.get_player(ctx)
		await player.teardown()
		await ctx.send("disconnected successfully.")

	#---------------play command-------------------------

	@commands.command(name = "play", aliases = ["p"])
	async def play_command(self, ctx, *, query: t.Optional[str]):
		player = self.get_player(ctx)

		if not player.is_connected:
			await player.connect(ctx)

		if query is None:
			if player.queue.is_empty:
				raise QueueIsEmpty

			if not player.is_paused:
				raise PlayerIsNotPaused

			await player.set_pause(False)
			await ctx.send("The music is resumed.")

		else:
			query = query.strip("<>")
			if not re.match("URL_REGEX", query):
				query = f"ytsearch:{query}"

			await player.add_tracks(ctx, await self.wavelink.get_tracks(query))

	@play_command.error
	async def play_command_error(self, ctx, exc):
		if isinstance(exc, QueueIsEmpty):
			await ctx.send("The queue is empty.")

		if isinstance(exc, PlayerIsNotPaused):
			await ctx.send("The music is not paused.")

		if isinstance(exc, NoVoiceChannel):
			await ctx.send("You must be in a voice channel.")

	#-----------------Pause command-----------------------

	@commands.command(name = "pause")
	async def pause_command(self, ctx):
		player = self.get_player(ctx)

		if player.is_paused:
			raise PlayerIsAlreadyPaused

		await player.set_pause(True)
		await ctx.send("The music is paused.")

	@pause_command.error
	async def pause_command_error(self, ctx, exc):
		if isinstance(exc, PlayerIsAlreadyPaused):
			await ctx.send("The music is already paused.")

	#-----------------Resume command----------------------

	@commands.command(name = "resume")
	async def resume_command(self, ctx):
		player = self.get_player(ctx)

		if not player.is_paused:
			raise PlayerIsNotPaused

		await player.set_pause(False)
		await ctx.send("The music is resumed.")

	@resume_command.error
	async def resume_command_error(self, ctx, exc):
		if isinstance(exc, PlayerIsNotPaused):
			await ctx.send("The music is not paused.")

	#-----------------Stop command------------------------

	@commands.command(name = "stop")
	async def stop_command(self, ctx):
		player = self.get_player(ctx)
		player.queue.empty()
		await player.stop()
		await ctx.send("Playback is stopped and the queue is deleted.")

	#-----------------Skip command------------------------

	@commands.command(name = "next", aliases = ["skip"])
	async def next_command(self, ctx):
		player = self.get_player(ctx)

		if not player.queue.upcoming:
			raise NoMoreTracks

		await player.stop()
		await ctx.send("Playing next track in the queue...")

	@next_command.error
	async def next_command_error(self, ctx, exc):
		if isinstance(exc, QueueIsEmpty):
			await ctx.send("Can't skip to the next song as the queue is empty.")
		
		if isinstance(exc, NoMoreTracks):
			await ctx.send("No more tracks in the queue to play.")

	#-----------------previous command--------------------

	@commands.command(name = "previous")
	async def previous_command(self, ctx):
		player = self.get_player(ctx)

		if not player.queue.history:
			raise NoPreviousTracks

		player.queue.position -= 2
		await player.stop()
		await ctx.send("Playing the previous track in the queue...")

	@previous_command.error
	async def previous_command_error(self, ctx, exc):
		if isinstance(exc, QueueIsEmpty):
			await ctx.send("Can't play the previous song as the queue is empty.")

		if isinstance(exc, NoPreviousTracks):
			await ctx.send("There is no previous track to play.")

	#-----------------Shuffle command---------------------

	@commands.command(name = "shuffle")
	async def shuffle_command(self, ctx):
		player = self.get_player(ctx)
		player.queue.shuffle()

		await ctx.send("The queue is shuffled!")

	@shuffle_command.error
	async def shuffle_command_error(self, ctx, exc):
		if isinstance(exc, QueueIsEmpty):
			await ctx.send("The queue could not be shuffled as it is currently empty!")

	#-----------------Repeat command----------------------

	@commands.command(name = "repeat")
	async def repeat_command(self, ctx, mode: str):
		if not mode in ("none", "1", "all"):
			raise InvalidRepeatMode

		player = self.get_player(ctx)
		player.queue.set_repeat_mode(mode)

		await ctx.send(f"The repeat mode has been set to `{mode}`.")

	#-----------------Queue command-----------------------

	@commands.command(name = "queue")
	async def queue_command(self, ctx, show: t.Optional[int] = 10):
		player = self.get_player(ctx)

		if player.queue.is_empty:
			raise QueueIsEmpty

		e = discord.Embed(
			title = "Queue",
			description = f"Showing upto next {show} tracks",
			color = ctx.author.color,
			timestamp = dt.datetime.utcnow()
		)
		e.set_author(name = "Query results")
		e.set_footer(text = f"Requested by {ctx.author.display_name}.", icon_url = ctx.author.avatar_url)
		e.add_field(
			name = "Currently playing",
			value = getattr(player.queue.current_track, "title", "No tracks currently playing."),
			inline = False
		)

		if upcoming := player.queue.upcoming:
			e.add_field(
				name = "Upcoming song",
				value = "\n".join(f"`{t.title}`" for t in upcoming[:show]),
				inline = False
			)

		msg = await ctx.send(embed = e)

	@queue_command.error
	async def queue_command_error(self, ctx, exc):
		if isinstance(exc, QueueIsEmpty):
			await ctx.send("The queue is currently empty!")

	#------------------Volume command------------------------

	@commands.group(name = "volume", aliases = ["v"], invoke_without_command = True)
	async def volume_group(self, ctx, volume: int):
		player = self.get_player(ctx)

		if volume < 0:
			raise VolumeTooLow

		if volume > 100:
			raise VolumeTooHigh

		await player.set_volume(volume)
		await ctx.send(f"Volume set to {volume:,}%")

	@volume_group.error
	async def volume_group_error(self, ctx, exc):
		if isinstance(exc, VolumeTooLow):
			await ctx.send("Volume must be 0% or above.")

		elif isinstance(exc, VolumeTooHigh):
			await ctx.send("Volume must be 100% or below.")

	@volume_group.command(name = "up", aliases = ["vup"])
	async def volume_up_command(self, ctx):
		player = self.get_player(ctx)

		if player.volume == 100:
			raise MaxVolume

		await player.set_volume(value := min(player.volume + 10, 100))
		await ctx.send(f"volume set to {value:,}%")

	@volume_up_command.error
	async def volume_up_command_error(self, ctx, exc):
		if isinstance(exc, MaxVolume):
			await ctx.send("The player is already at max volume.")

	@volume_group.command(name = "down", aliases = ["vdown"])
	async def volume_down_command(self, ctx):
		player = self.get_player(ctx)

		if player.volume == 0:
			raise MinVolume

		await player.set_volume(value := max(0, player.volume - 10))
		await ctx.send(f"volume set to {value:,}%")

	@volume_down_command.error
	async def volume_down_command_error(self, ctx, exc):
		if isinstance(exc, MinVolume):
			await ctx.send("The player is already at min volume.")

	#---------------------Lyrics command--------------------------
	@commands.command(name = "lyrics")
	async def lyrics_command(self, ctx, name: t.Optional[str]):
		player = self.get_player(ctx)
		name = name or player.queue.current_track.title

		async with ctx.typing():
			async with aiohttp.request("GET", LYRICS_URL + name, headers = {}) as r:
				if not 200 <= r.status <= 299:
					raise NoLyricsFound

				data = await r.json()

				if len(data["lyrics"]) > 2000:
					return await ctx.send(f"<{data['links']['genius']}>")

				e = discord.Embed(
					title = data["title"],
					description = data["lyrics"],
					color = ctx.author.color,
					timestamp = dt.datetime.utcnow()
				)

				e.set_thumbnail(url = data["thumbnail"]["genius"])
				e.set_author(name = data["author"])
				await ctx.send(embed = e)

	@lyrics_command.error
	async def lyrics_command_error(self, ctx, exc):
		if isinstance(exc, NoLyricsFound):
			await ctx.send("No lyrics found for the song.")

	#-----------------------Equaliser command----------------------------

	@commands.command(name = "eq")
	async def eq_command(self, ctx, preset: str):
		player = self.get_player(ctx)

		eq = getattr(wavelink.eqs.Equalizer, preset, None)
		if not eq:
			raise InvalidEQPreset

		await player.set_eq(eq())
		await ctx.send(f"Equalizer adjusted to the {preset} preset.")

	@eq_command.error
	async def eq_command_error(self, ctx, exc):
		if isinstance(exc, InvalidEQPreset):
			await ctx.send(f"The eq must be either: \n1. **flat**\n2. **boost**\n3. **metal**\n4. **piano**")

	#-----------------------Advanced Equaliser command---------------------

	@commands.command(name = "adveq", aliases = ["aeq"])
	async def adveq_command(self, ctx, band: int, gain: float):
		player = self,get_player(ctx)

		if not 1 <= band <= 15 and band not in HZ_BANDS:
			raise NonExistentEQBand

		if band > 15:
			band = HZ_BANDS.index(band) + 1

		if abs(gain) > 10:
			raise EQGainOutOfBounds

		player.eq_levels[band - 1] = gain/10
		eq = wavelink.eqs.Equalizer(levels = [(i, gain) for i, gain in enumerate(player.eq_levels)])
		await player.set_eq(eq)
		await ctx.send("Equalizer adjusted.")

	@adveq_command.error
	async def adveq_command_error(self, ctx, exc):
		if isinstance(exc, NonExistentEQBand):
			await ctx.send(
                "This is a 15 band equaliser -- the band number should be between 1 and 15, or one of the following "
                "frequencies: " + ", ".join(str(b) for b in HZ_BANDS)
            )

		elif isinstance(exc, EQGainOutOfBounds):
			await ctx.send("The EQ gain for any band should be between 10 dB and -10 dB.")

def setup(bot):
	bot.add_cog(Music(bot))