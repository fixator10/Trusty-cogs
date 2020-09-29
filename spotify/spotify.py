import discord
import re
import tekore
import logging
import asyncio
import time

from copy import copy
from typing import Tuple, Optional, Literal, Mapping

from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import humanize_list
from redbot.core.i18n import Translator, cog_i18n

from .helpers import (
    NotPlaying,
    SPOTIFY_RE,
    LOOKUP,
    ACTION_EMOJIS,
    SCOPE,
    SearchTypes,
    SpotifyURIConverter,
    RecommendationsConverter,
)
from .menus import (
    SpotifyUserMenu,
    SpotifyPages,
    SpotifySearchMenu,
    SpotifyTrackPages,
    SpotifyBaseMenu,
    SpotifyPlaylistsPages,
    SpotifyPlaylistPages,
    SpotifyTopTracksPages,
    SpotifyTopArtistsPages,
    SpotifyRecentSongPages,
    SpotifyArtistPages,
    SpotifyAlbumPages,
    SpotifyShowPages,
    SpotifyEpisodePages,
    SpotifyNewPages,
)

log = logging.getLogger("red.trusty-cogs.spotify")
_ = Translator("Spotify", __file__)

TokenConverter = commands.get_dict_converter(delims=[" ", ",", ";"])


@cog_i18n(_)
class Spotify(commands.Cog):
    """
    Display information from Spotify's API
    """

    __author__ = ["TrustyJAID"]
    __version__ = "1.3.4"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=218773382617890828)
        self.config.register_user(token={}, listen_for=[], show_private=False)
        self._app_token = None
        self._tokens: Tuple[str] = None
        self._spotify_client = None
        self._sender = None
        self._credentials = None
        self._ready = asyncio.Event()
        self.bot.loop.create_task(self.initialize())
        self.HAS_TOKENS = False
        self.current_menus = {}

    async def initialize(self):
        tokens = await self.bot.get_shared_api_tokens("spotify")
        if not tokens:
            self._ready.set()
            return
        try:
            self._sender = tekore.AsyncSender()
            self._tokens = (
                tokens.get("client_id"),
                tokens.get("client_secret"),
                tokens.get("redirect_uri", "https://localhost/"),
            )
            self._credentials = tekore.Credentials(*self._tokens, sender=self._sender)
            self._app_token = tekore.request_client_token(*self._tokens[:2])
            self._spotify_client = tekore.Spotify(self._app_token, sender=self._sender)
        except KeyError:
            log.exception("error starting the cog")
        self._ready.set()

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """
        Thanks Sinbad!
        """
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nCog Version: {self.__version__}"

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        await self._ready.wait()

    def cog_unload(self):
        if self._sender:
            self.bot.loop.create_task(self._sender.client.aclose())

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        """
        Method for finding users data inside the cog and deleting it.
        """
        await self.config.user_from_id(user_id).clear()

    async def get_user_auth(self, ctx: commands.Context, user: Optional[discord.User] = None):
        """
        Handles getting and saving user authorization information
        """
        author = user or ctx.author
        if not self._credentials:
            await ctx.send(
                _(
                    "The bot owner needs to set their Spotify credentials "
                    "before this command can be used."
                    " See `{prefix}spotify set creds` for more details."
                ).format(prefix=ctx.clean_prefix)
            )
            return
        user_tokens = await self.config.user(author).token()
        if user_tokens:
            user_tokens["expires_in"] = user_tokens["expires_at"] - int(time.time())
            user_token = tekore.Token(user_tokens, user_tokens["uses_pkce"])
            if user_token.is_expiring:
                try:
                    user_token = await self._credentials.refresh(user_token)
                except tekore.BadRequest:
                    await ctx.send("Your refresh token has been revoked, clearing data.")
                    await self.config.user(ctx.author).token.clear()
                    return
                await self.save_token(author, user_token)
            return user_token

        auth = tekore.UserAuth(self._credentials, scope=SCOPE)
        msg = _(
            "Please accept the authorization in the following link and reply "
            "to me with the full url\n\n {auth}"
        ).format(auth=auth.url)

        def check(message):
            return message.author.id == author.id and self._tokens[-1] in message.content

        try:
            await author.send(msg)
            # pred = MessagePredicate.same_context(user=ctx.author)
        except discord.errors.Forbidden:
            # pre = MessagePredicate.same_context(ctx)
            await ctx.send(msg)
        try:
            check_msg = await ctx.bot.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            await ctx.send(_("Alright I won't interact with spotify for you."))
            return
        redirected = check_msg.clean_content.strip()
        if self._tokens[-1] not in redirected:
            return await ctx.send(_("Credentials not valid"))
        reply_msg = _("Your authorization has been set!")
        try:
            await author.send(reply_msg)
            # pred = MessagePredicate.same_context(user=ctx.author)
        except discord.errors.Forbidden:
            # pre = MessagePredicate.same_context(ctx)
            await ctx.send(reply_msg)

        user_token = await auth.request_token(url=redirected)
        await self.save_token(ctx.author, user_token)

        return user_token

    async def save_token(self, author: discord.User, user_token: tekore.Token):
        async with self.config.user(author).token() as token:
            token["access_token"] = user_token.access_token
            token["refresh_token"] = user_token.refresh_token
            token["expires_at"] = user_token.expires_at
            token["scope"] = str(user_token.scope)
            token["uses_pkce"] = user_token.uses_pkce
            token["token_type"] = user_token.token_type

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        Handles listening for reactions and parsing
        """
        if payload.message_id in self.current_menus:
            if self.current_menus[payload.message_id] == payload.user_id:
                log.debug("Menu reaction from the same user ignoring")
                return
        if str(payload.emoji) not in LOOKUP.keys():
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        listen_for = await self.config.user_from_id(payload.user_id).listen_for()
        if not listen_for:
            return
        channel = self.bot.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return

        action = LOOKUP[str(payload.emoji)]
        if action == "play":
            # play the song if it exists
            content = message.content
            if message.embeds:
                content += " ".join(
                    v
                    for k, v in message.embeds[0].to_dict().items()
                    if k in ["title", "description"]
                )
            song_data = SPOTIFY_RE.finditer(content)
            tracks = []
            new_uri = ""
            if song_data:
                for match in song_data:
                    new_uri = f"spotify:{match.group(2)}:{match.group(3)}"
                    if match.group(2) == "track":
                        tracks.append(match.group(3))
            ctx = await self.bot.get_context(message)
            user = self.bot.get_user(payload.user_id)
            if not user:
                return
            user_token = await self.get_user_auth(ctx, user)
            if not user_token:
                return
            try:
                user_spotify = tekore.Spotify(sender=self._sender)
                with user_spotify.token_as(user_token):
                    if tracks:
                        await user_spotify.playback_start_tracks(tracks)
                        return
                    if not tracks and new_uri:
                        await user_spotify.playback_start_context(new_uri)
                        return
                    elif message.embeds:
                        em = message.embeds[0]
                        if em.description:
                            look = f"{em.title if em.title else ''}-{em.description}"
                            find = re.search(r"\[(.+)\]", look)
                            if find:
                                query = find.group(1)
                        else:
                            query = em.title if em.title else ""
                        log.debug(query)
                        if not query or query == "-":
                            return
                        search = await user_spotify.search(query, limit=50)
                        tracks = search[0].items
                        if tracks:
                            await user_spotify.playback_start_tracks([t.id for t in tracks])
            except Exception:
                log.exception("Error on reaction add play")
                return
        if action == "like":
            content = message.content
            if message.embeds:
                content += " ".join(
                    v
                    for k, v in message.embeds[0].to_dict().items()
                    if k in ["title", "description"]
                )
            song_data = SPOTIFY_RE.finditer(content)
            tracks = []
            albums = []
            playlists = []
            if song_data:
                for match in song_data:
                    if match.group(2) == "track":
                        tracks.append(match.group(3))
                    if match.group(2) == "album":
                        albums.append(match.group(3))
                    if match.group(2) == "playlist":
                        playlists.append(match.group(3))
            ctx = await self.bot.get_context(message)
            user = self.bot.get_user(payload.user_id)
            if not user:
                return
            user_token = await self.get_user_auth(ctx, user)
            if not user_token:
                return
            try:
                user_spotify = tekore.Spotify(sender=self._sender)
                with user_spotify.token_as(user_token):
                    if tracks:
                        await user_spotify.saved_tracks_add(tracks)
                    if albums:
                        await user_spotify.saved_albums_add(albums)
                    if playlists:
                        for playlist in playlists:
                            await user_spotify.playlists_add(playlist)
            except Exception:
                return

    @commands.Cog.listener()
    async def on_red_api_tokens_update(
        self, service_name: str, api_tokens: Mapping[str, str]
    ) -> None:
        if service_name == "spotify":
            await self.initialize()

    @commands.group(name="spotify", aliases=["sp"])
    async def spotify_com(self, ctx: commands.Context):
        """
        Spotify commands
        """
        pass

    @spotify_com.group(name="set")
    async def spotify_set(self, ctx: commands.Context):
        """
        Setup Spotify cog
        """
        pass

    @spotify_com.group(name="playlist", aliases=["playlists"])
    async def spotify_playlist(self, ctx: commands.Context):
        """
        View Spotify Playlists
        """
        pass

    @spotify_com.group(name="artist", aliases=["artists"])
    async def spotify_artist(self, ctx: commands.Context):
        """
        View Spotify Artist info
        """
        pass

    @spotify_set.command(name="listen")
    async def set_reaction_listen(self, ctx: commands.Context, *listen_for: str):
        """
        Set the bot to listen for specific emoji reactions on messages

        If the message being reacted to has somthing valid to search
        for the bot will attempt to play the found search on spotify for you.

        `<listen_for>` Must be either `play` or `like`

        \N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16} will look only for spotify links and add them to your liked songs
        \N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16} will attempt to play the song found by searching the message content
        """
        allowed = [
            "play",
            "like",
            "\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}",
            "\N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}",
        ]
        if not any([i in allowed for i in listen_for]):
            return await ctx.send(
                _(
                    "One of the values you supplied for `listen_for` is not valid. "
                    "Only `play` and `like` are accepted."
                )
            )
        added = []
        removed = []
        async with self.config.user(ctx.author).listen_for() as current:
            for listen in listen_for:
                _to_set = listen
                if listen in LOOKUP:
                    _to_set = LOOKUP[listen]
                if _to_set in current:
                    current.remove(_to_set)
                    removed.append(_to_set)
                else:
                    current.append(_to_set)
                    added.append(_to_set)
        add = _("I will now listen for {adds} reactions.\n").format(adds=humanize_list(added))
        remove = _("I will stop listening for {rem} reactions.\n").format(
            rem=humanize_list(removed)
        )
        to_send = ""
        if added:
            to_send += add
        if removed:
            to_send += remove
        await ctx.send(to_send)

    @spotify_set.command(name="showprivate")
    async def show_private(self, ctx: commands.Context, show_private: bool):
        """
        Set whether or not to show private playlists

        This will also display your spotify username and a link
        to your profile if you use `[p]spotify me` command in public channels.
        """
        await self.config.user(ctx.author).show_private.set(show_private)
        if show_private:
            msg = _("I will show private playlists now.")
        else:
            msg = _("I will stop showing private playlists now.")
        await ctx.send(msg)

    @spotify_set.command(name="creds")
    @commands.is_owner()
    async def command_audioset_spotifyapi(self, ctx: commands.Context):
        """Instructions to set the Spotify API tokens."""
        message = _(
            "1. Go to Spotify developers and log in with your Spotify account.\n"
            "(https://developer.spotify.com/dashboard/applications)\n"
            '2. Click "Create An App".\n'
            "3. Fill out the form provided with your app name, etc.\n"
            '4. When asked if you\'re developing commercial integration select "No".\n'
            "5. Accept the terms and conditions.\n"
            "6. Copy your client ID and your client secret into:\n"
            "`{prefix}set api spotify client_id <your_client_id_here> "
            "client_secret <your_client_secret_here>`\n"
            "You may also provide `redirect_uri` in this command with "
            "a different redirect you would like to use but this is optional. "
            "the default redirect_uri is https://localhost/\n\n"
            "Note: The redirect URI Must be set in the Spotify Dashboard and must "
            "match either `https://localhost/` or the one you set with the `[p]set api` command"
        ).format(prefix=ctx.prefix)
        await ctx.maybe_send_embed(message)

    @spotify_set.command(name="forgetme")
    async def spotify_forgetme(self, ctx: commands.Context):
        """
        Forget all your spotify settings and credentials on the bot
        """
        await self.config.user(ctx.author).clear()
        await ctx.send(_("All your spotify data deleted from my settings."))

    @spotify_com.command(name="me")
    @commands.bot_has_permissions(embed_links=True)
    async def spotify_me(self, ctx: commands.Context):
        """
        Shows your current Spotify Settings
        """
        em = discord.Embed(color=discord.Colour(0x1DB954))
        em.set_author(
            name=ctx.author.display_name + _(" Spotify Profile"), icon_url=ctx.author.avatar_url
        )
        msg = ""
        cog_settings = await self.config.user(ctx.author).all()
        listen_emojis = humanize_list([ACTION_EMOJIS[i] for i in cog_settings["listen_for"]])
        if not listen_emojis:
            listen_emojis = "Nothing"
        show_private = cog_settings["show_private"]
        msg += _("Watching for Emojis: {listen_emojis}\n").format(listen_emojis=listen_emojis)
        msg += _("Show Private Playlists: {show_private}\n").format(show_private=show_private)
        if not cog_settings["token"]:
            em.description = msg
            await ctx.send(embed=em)
            return
        user_token = await self.get_user_auth(ctx)
        if user_token:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                cur = await user_spotify.current_user()
        if show_private or isinstance(ctx.channel, discord.DMChannel):
            msg += _(
                "Spotify Name: [{display_name}](https://open.spotify.com/user/{user_id})\n"
                "Subscription: {product}\n"
            ).format(display_name=cur.display_name, product=cur.product, user_id=cur.id)
        if isinstance(ctx.channel, discord.DMChannel):
            private = _("Country: {country}\nSpotify ID: {id}\nEmail: {email}\n").format(
                country=cur.country, id=cur.id, email=cur.email
            )
            em.add_field(name=_("Private Data"), value=private)

        em.description = msg
        await ctx.send(embed=em)

    @spotify_com.command(name="now", aliases=["np"])
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_now(self, ctx: commands.Context, detailed: Optional[bool] = False):
        """
        Displays your currently played spotify song
        """

        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            await SpotifyUserMenu(
                source=SpotifyPages(user_token=user_token, sender=self._sender, detailed=detailed),
                delete_message_after=False,
                clear_reactions_after=True,
                timeout=60,
                cog=self,
                user_token=user_token,
            ).start(ctx=ctx)
        except NotPlaying:
            await ctx.send(_("It appears you're not currently listening to Spotify."))

    @spotify_com.command(name="share")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_share(self, ctx: commands.Context):
        """
        Tell the bot to play the users current song in their current voice channel
        """

        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                cur = await user_spotify.playback()
                if cur.is_playing and not cur.item.is_local:
                    msg = copy(ctx.message)
                    msg.content = ctx.prefix + f"play {cur.item.uri}"
                    self.bot.dispatch("message", msg)
                    await ctx.tick()
                else:
                    return await ctx.send(
                        _("You don't appear to be listening to something I can play in audio.")
                    )
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="search")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_search(
        self,
        ctx: commands.Context,
        detailed: Optional[bool] = False,
        search_type: Optional[SearchTypes] = "track",
        *,
        query: str,
    ):
        """
        Search Spotify for things to play

        `[detailed=False]` Show detailed information for individual tracks.
        `[search_type=track]` The search type, available options are:
         - `track(s)`
         - `artist(s)`
         - `album(s)`
         - `playlist(s)`
         - `show(s)`
         - `episode(s)`
        `<query>` What you want to search for.
        """
        search_types = {
            "track": SpotifyTrackPages,
            "artist": SpotifyArtistPages,
            "album": SpotifyAlbumPages,
            "episode": SpotifyEpisodePages,
            "playlist": SpotifyPlaylistPages,
            "show": SpotifyShowPages,
        }
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            search = await user_spotify.search(query, (search_type,), limit=50)
            items = search[0].items
        if not search[0].items:
            return await ctx.send(
                _("No {search_type} could be found matching that query.").format(
                    search_type=search_type
                )
            )
        await SpotifySearchMenu(
            source=search_types[search_type](items=items, detailed=detailed),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_com.command(name="recommendations", aliases=["recommend", "recommendation"])
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_recommendations(
        self,
        ctx: commands.Context,
        detailed: Optional[bool] = False,
        *,
        recommendations: RecommendationsConverter = {},
    ):
        """
        Get Spotify Recommendations

        `<recommendations>` Requires at least 1 of the following matching objects:
         - `genre` Must be a valid genre type.
         - `tracks` Any spotify URL or URI leading to tracks will be added to the seed
         - `artists` Any spotify URL or URI leading to artists will be added to the seed
         The following parameters also exist and must include some additional parameter:
         - `acousticness` + a value from 0-100
         - `danceability` + a value from 0-100
         - `duration_ms` the duration target of the tracks
         - `energy` + a value from 0-100
         - `instrumentalness` + a value from 0-100
         - `key` A value from 0-11 representing Pitch Class notation
         - `liveness` + a value from 0-100
         - `loudness` A value from -60 to 0 represending dB
         - `mode` either major or minor
         - `popularity` + a value from 0-100
         - `speechiness` + a value from 0-100
         - `tempo` the tempo in BPM
         - `time_signature` the measure of bars
         - `valence` + a value from 0-100
        """

        log.debug(recommendations)
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            try:
                search = await user_spotify.recommendations(**recommendations)
            except Exception:
                log.exception("Error getting recommendations")
                return await ctx.send(
                    _("I could not find any recommendations with those parameters")
                )
            items = search.tracks
        if not items:
            return await ctx.send(_("No recommendations could be found that query."))
        await SpotifySearchMenu(
            source=SpotifyTrackPages(items=items, detailed=detailed),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_com.command(name="recent")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_recently_played(
        self, ctx: commands.Context, detailed: Optional[bool] = False
    ):
        """
        Displays your most recently played songs on Spotify
        """

        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            search = await user_spotify.playback_recently_played(limit=50)
            tracks = search.items
        await SpotifySearchMenu(
            source=SpotifyRecentSongPages(tracks=tracks, detailed=detailed),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_com.command(name="toptracks")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def top_tracks(self, ctx: commands.Context):
        """
        List your top tracks on spotify
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            cur = await user_spotify.current_user_top_tracks(limit=50)

        tracks = cur.items
        await SpotifyBaseMenu(
            source=SpotifyTopTracksPages(tracks),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_com.command(name="topartists")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def top_artists(self, ctx: commands.Context):
        """
        List your top tracks on spotify
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            cur = await user_spotify.current_user_top_artists(limit=50)

        artists = cur.items
        await SpotifyBaseMenu(
            source=SpotifyTopArtistsPages(artists),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_com.command(name="new")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_new(self, ctx: commands.Context):
        """
        List new releases on Spotify
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            playlists = await user_spotify.new_releases(limit=50)
        playlist_list = playlists.items
        await SpotifySearchMenu(
            source=SpotifyNewPages(playlist_list),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_com.command(name="pause")
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_pause(self, ctx: commands.Context):
        """
        Pauses spotify for you
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                await user_spotify.playback_pause()
            await ctx.message.add_reaction("\N{DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}")
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="resume")
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_resume(self, ctx: commands.Context):
        """
        Resumes spotify for you
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                cur = await user_spotify.playback()
                if not cur or not cur.is_playing:
                    await user_spotify.playback_resume()
                else:
                    return await ctx.send(_("You are already playing music on Spotify."))
            await ctx.message.add_reaction(
                "\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}"
            )
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="next", aliases=["skip"])
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_next(self, ctx: commands.Context):
        """
        Skips to the next track in queue on Spotify
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                await user_spotify.playback_next()
            await ctx.message.add_reaction(
                "\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}\N{VARIATION SELECTOR-16}"
            )
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="previous", aliases=["prev"])
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_previous(self, ctx: commands.Context):
        """
        Skips to the previous track in queue on Spotify
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                await user_spotify.playback_previous()
            await ctx.message.add_reaction(
                "\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}\N{VARIATION SELECTOR-16}"
            )
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="play")
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_play(
        self, ctx: commands.Context, *, url_or_playlist_name: Optional[str] = ""
    ):
        """
        Play a track, playlist, or album on Spotify

        `<url_or_playlist_name>` can be multiple spotify track URL *or* URI or
        a single album or playlist link

        if something other than a spotify URL or URI is provided
        the bot will search through your playlists and start playing
        the playlist with the closest matching name
        """
        song_data = SPOTIFY_RE.finditer(url_or_playlist_name)
        tracks = []
        new_uri = ""
        if song_data:
            for match in song_data:
                new_uri = f"spotify:{match.group(2)}:{match.group(3)}"
                if match.group(2) == "track":
                    tracks.append(match.group(3))
            log.debug(new_uri)
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                if tracks:
                    await user_spotify.playback_start_tracks(tracks)
                    await ctx.message.add_reaction(
                        "\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}"
                    )
                    return
                if new_uri:
                    await user_spotify.playback_start_context(new_uri)
                    await ctx.message.add_reaction(
                        "\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}"
                    )
                    return
                if url_or_playlist_name:
                    cur = await user_spotify.followed_playlists(limit=50)
                    playlists = cur.items
                    while len(playlists) < cur.total:
                        new = await user_spotify.followed_playlists(
                            limit=50, offset=len(playlists)
                        )
                        for p in new.items:
                            playlists.append(p)
                    for playlist in playlists:
                        if url_or_playlist_name.lower() in playlist.name.lower():
                            await user_spotify.playback_start_context(playlist.uri)
                            await ctx.message.add_reaction(
                                "\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}"
                            )
                            return
                    tracks = await user_spotify.saved_tracks(limit=50)
                    for track in tracks.items:
                        if (
                            url_or_playlist_name.lower() in track.track.name.lower()
                            or url_or_playlist_name.lower()
                            in ", ".join(a.name for a in track.track.artists)
                        ):
                            await user_spotify.playback_start_tracks([track.track.id])
                            await ctx.message.add_reaction(
                                "\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}"
                            )
                            return
                else:
                    cur = await user_spotify.saved_tracks(limit=50)
                    await user_spotify.playback_start_tracks([t.track.id for t in cur.items])
                    await ctx.message.add_reaction(
                        "\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}"
                    )
                    return
                await ctx.send(_("I could not find any URL's or matching playlist names."))
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="queue")
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_queue_add(self, ctx: commands.Context, song: SpotifyURIConverter):
        """
        Queue a song to play next in Spotify

        `<song>` is a spotify track URL or URI for the song to add to the queue
        """
        # song_data = SPOTIFY_RE.match(song)
        # if not song_data:
        # return await ctx.send(_("That does not look like a spotify link."))
        if song.group(2) != "track":
            return await ctx.send(_("I can only append 1 track at a time right now to the queue."))
        new_uri = f"spotify:{song.group(2)}:{song.group(3)}"
        log.debug(new_uri)
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                await user_spotify.playback_queue_add(new_uri)
            await ctx.message.add_reaction(
                "\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16}"
            )
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="repeat")
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_repeat(self, ctx: commands.Context, state: Optional[str]):
        """
        Repeats your current song on spotify

        `<state>` must accept one of `off`, `track`, or `context`.
        """
        if state and state.lower() not in ["off", "track", "context"]:
            return await ctx.send(_("Repeat must accept either `off`, `track`, or `context`."))
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                if not state:
                    cur = await user_spotify.playback()
                    if not cur:
                        return await ctx.send(
                            _("I could not find an active device to send requests for.")
                        )
                    if cur.repeat_state == "off":
                        state = "track"
                    if cur.repeat_state == "track":
                        state = "context"
                    if cur.repeat_state == "context":
                        state = "off"
                await user_spotify.playback_repeat(state.lower())
            await ctx.message.add_reaction(
                "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}"
            )
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="shuffle")
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_shuffle(self, ctx: commands.Context, state: Optional[bool] = None):
        """
        Shuffles your current song list

        `<state>` either true or false. Not providing this will toggle the current setting.
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                if state is None:
                    cur = await user_spotify.playback()
                    if not cur:
                        await ctx.send(
                            _("I could not find an active device to send requests for.")
                        )
                    state = not cur.shuffle_state
                await user_spotify.playback_shuffle(state)
            await ctx.message.add_reactions("\N{TWISTED RIGHTWARDS ARROWS}")
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="seek")
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_seek(self, ctx: commands.Context, time: int):
        """
        Seek to a specific point in the current song

        `<time>` position inside the current song to skip to.
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                await user_spotify.playback_seek(int(time * 1000))
            await ctx.message.add_reactions(
                "\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE}\N{VARIATION SELECTOR-16}"
            )
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="volume")
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_volume(self, ctx: commands.Context, volume: int):
        """
        Set your spotify volume percentage

        `<volume>` a number between 0 and 100 for volume percentage.
        """
        volume = max(min(100, volume), 0)  # constrains volume to be within 100
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                cur = await user_spotify.playback()
                await user_spotify.playback_volume(volume)
                if volume == 0:
                    await ctx.message.add_reaction("\N{SPEAKER WITH CANCELLATION STROKE}")
                elif cur and volume > cur.device.volume_percent:
                    await ctx.message.add_reaction("\N{SPEAKER WITH THREE SOUND WAVES}")
                else:
                    await ctx.message.add_reaction("\N{SPEAKER WITH ONE SOUND WAVE}")
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_com.command(name="device", hidden=True)
    @commands.bot_has_permissions(add_reactions=True)
    async def spotify_device(self, ctx: commands.Context, *, device_name: str):
        """
        Change the currently playing spotify device

        `<device_name>` The name of the device you want to switch to.
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            is_playing = False
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                devices = await user_spotify.playback_devices()
                now = await user_spotify.playback()
                if now and now.is_playing:
                    is_playing = True
            for d in devices:
                if device_name.lower() in d.name.lower():
                    await user_spotify.playback_transfer(d.id, True)
                    break
            await ctx.tick()
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_playlist.command(name="featured")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_playlist_featured(self, ctx: commands.Context):
        """
        List your Spotify featured Playlists
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            playlists = await user_spotify.featured_playlists(limit=50)
        playlist_list = playlists[1].items
        await SpotifySearchMenu(
            source=SpotifyNewPages(playlist_list),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_playlist.command(name="list", aliases=["ls"])
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def playlist_playlist_list(self, ctx: commands.Context):
        """
        List your Spotify Playlists

        If this command is done in DM with the bot it will show private playlists
        otherwise this will not display private playlists unless showprivate
        has been toggled on.
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            cur = await user_spotify.followed_playlists(limit=50)
            playlists = cur.items
            while len(playlists) < cur.total:
                new = await user_spotify.followed_playlists(limit=50, offset=len(playlists))
                for p in new.items:
                    playlists.append(p)
        show_private = await self.config.user(ctx.author).show_private() or isinstance(
            ctx.channel, discord.DMChannel
        )
        if show_private:
            playlist_list = playlists
        else:
            playlist_list = [p for p in playlists if p.public is not False]
        await SpotifyBaseMenu(
            source=SpotifyPlaylistsPages(playlist_list),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_playlist.command(name="view")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_playlist_view(self, ctx: commands.Context):
        """
        View details about your spotify playlists

        If this command is done in DM with the bot it will show private playlists
        otherwise this will not display private playlists unless showprivate
        has been toggled on.
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            cur = await user_spotify.followed_playlists(limit=50)
            playlists = cur.items
            while len(playlists) < cur.total:
                new = await user_spotify.followed_playlists(limit=50, offset=len(playlists))
                for p in new.items:
                    playlists.append(p)
        show_private = await self.config.user(ctx.author).show_private() or isinstance(
            ctx.channel, discord.DMChannel
        )
        show_private = await self.config.user(ctx.author).show_private() or isinstance(
            ctx.channel, discord.DMChannel
        )
        if show_private:
            playlist_list = playlists.items
        else:
            playlist_list = [p for p in playlists if p.public is not False]
        await SpotifySearchMenu(
            source=SpotifyPlaylistPages(playlist_list),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)

    @spotify_playlist.command(name="create")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_playlist_create(
        self,
        ctx: commands.Context,
        name: str,
        public: Optional[bool] = False,
        *,
        description: Optional[str] = "",
    ):
        """
        Create a Spotify Playlist

        `<name>` The name of the newly created playlist
        `[public]` Wheter or not the playlist should be public, defaults to False.
        `[description]` The description of the playlist you're making.
        """
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                user = await user_spotify.current_user()
                await user_spotify.playlist_create(user.id, name, public, description)
                await ctx.tick()
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_playlist.command(name="add")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_playlist_add(
        self,
        ctx: commands.Context,
        name: str,
        *to_add: SpotifyURIConverter,
    ):
        """
        Add 1 (or more) tracks to a spotify playlist

        `<name>` The name of playlist you want to add songs to
        `<to_remove>` The song links or URI's you want to add
        """
        tracks = []
        new_uri = ""
        for match in to_add:
            new_uri = f"spotify:{match.group(2)}:{match.group(3)}"
            if match.group(2) == "track":
                tracks.append(new_uri)
        if not tracks:
            return await ctx.send(
                _("You did not provide any tracks for me to add to the playlist.")
            )
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                cur = await user_spotify.followed_playlists(limit=50)
                playlists = cur.items
                while len(playlists) < cur.total:
                    new = await user_spotify.followed_playlists(limit=50, offset=len(playlists))
                    for p in new.items:
                        playlists.append(p)
                for playlist in playlists:
                    if name.lower() == playlist.name.lower():
                        await user_spotify.playlist_add(playlist.id, tracks)
                        await ctx.tick()
                        return
            await ctx.send(_("I could not find a playlist matching {name}.").format(name=name))
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_playlist.command(name="remove")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_playlist_remove(
        self,
        ctx: commands.Context,
        name: str,
        *to_remove: SpotifyURIConverter,
    ):
        """
        Remove 1 (or more) tracks to a spotify playlist

        `<name>` The name of playlist you want to remove songs from
        `<to_remove>` The song links or URI's you want to have removed
        """
        tracks = []
        new_uri = ""
        for match in to_remove:
            new_uri = f"spotify:{match.group(2)}:{match.group(3)}"
            if match.group(2) == "track":
                tracks.append(new_uri)
        if not tracks:
            return await ctx.send(
                _("You did not provide any tracks for me to add to the playlist.")
            )
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                cur = await user_spotify.followed_playlists(limit=50)
                playlists = cur.items
                while len(playlists) < cur.total:
                    new = await user_spotify.followed_playlists(limit=50, offset=len(playlists))
                    for p in new.items:
                        playlists.append(p)
                for playlist in playlists:
                    if name.lower() == playlist.name.lower():
                        await user_spotify.playlist_remove(playlist.id, tracks)
                        await ctx.tick()
                        return
            await ctx.send(_("I could not find a playlist matching {name}.").format(name=name))
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_playlist.command(name="follow")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_playlist_follow(
        self,
        ctx: commands.Context,
        public: Optional[bool] = False,
        *to_follow: SpotifyURIConverter,
    ):
        """
        Add a playlist to your spotify library

        `[public]` Whether or not the followed playlist should be public after
        `<to_follow>` The song links or URI's you want to have removed
        """
        tracks = []
        new_uri = ""
        for match in to_follow:
            new_uri = f"spotify:{match.group(2)}:{match.group(3)}"
            if match.group(2) == "playlist":
                tracks.append(match.group(3))
        if not tracks:
            return await ctx.send(
                _("You did not provide any playlists for me to add to your library.")
            )
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                for playlist in tracks:
                    await user_spotify.playlist_follow(playlist, public)
                await ctx.tick()
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_artist.command(name="follow")
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_artist_follow(
        self,
        ctx: commands.Context,
        *to_follow: SpotifyURIConverter,
    ):
        """
        Add an artist to your spotify library

        `<to_follow>` The song links or URI's you want to have removed
        """
        tracks = []
        new_uri = ""
        for match in to_follow:
            new_uri = f"spotify:{match.group(2)}:{match.group(3)}"
            if match.group(2) == "artist":
                tracks.append(match.group(3))
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        try:
            user_spotify = tekore.Spotify(sender=self._sender)
            with user_spotify.token_as(user_token):
                for playlist in tracks:
                    await user_spotify.artist_follow(playlist)
                await ctx.tick()
        except tekore.NotFound:
            await ctx.send(_("I could not find an active device to send requests for."))
        except tekore.Forbidden as e:
            if "non-premium" in str(e):
                await ctx.send(_("This action is prohibited for non-premium users."))
            else:
                await ctx.send(_("I couldn't perform that action for you."))
        except tekore.HTTPError:
            log.exception("Error grabing user info from spotify")
            await ctx.send(
                _("An exception has occured, please contact the bot owner for more assistance.")
            )

    @spotify_artist.command(name="albums", aliases=["album"])
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def spotify_artist_albums(
        self,
        ctx: commands.Context,
        *to_follow: SpotifyURIConverter,
    ):
        """
        View an artists albums

        `<to_follow>` The artis links or URI's you want to view the albums of
        """
        tracks = []
        new_uri = ""
        for match in to_follow:
            new_uri = f"spotify:{match.group(2)}:{match.group(3)}"
            if match.group(2) == "artist":
                tracks.append(match.group(3))
        if not tracks:
            return await ctx.send(_("You did not provide an artist link or URI."))
        user_token = await self.get_user_auth(ctx)
        if not user_token:
            return await ctx.send(_("You need to authorize me to interact with spotify."))
        user_spotify = tekore.Spotify(sender=self._sender)
        with user_spotify.token_as(user_token):
            search = await user_spotify.artist_albums(tracks[0], limit=50)
            tracks = search.items
        await SpotifySearchMenu(
            source=SpotifyAlbumPages(tracks, False),
            delete_message_after=False,
            clear_reactions_after=True,
            timeout=60,
            cog=self,
            user_token=user_token,
        ).start(ctx=ctx)