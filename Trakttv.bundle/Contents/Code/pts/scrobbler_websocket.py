from core.eventing import EventManager
from core.helpers import get_pref
from core.logger import Logger
from data.watch_session import WatchSession
from plex.plex_media_server import PlexMediaServer
from plex.plex_metadata import PlexMetadata
from pts.scrobbler import Scrobbler, ScrobblerMethod


log = Logger('pts.scrobbler_websocket')


class WebSocketScrobbler(ScrobblerMethod):
    name = 'WebSocketScrobbler'

    def __init__(self):
        super(WebSocketScrobbler, self).__init__()

        EventManager.subscribe('notifications.playing', self.update)

    @classmethod
    def test(cls):
        if PlexMediaServer.get_sessions() is None:
            log.info("Error while retrieving sessions, assuming WebSocket method isn't available")
            return False

        server_info = PlexMediaServer.get_info()
        if server_info is None:
            log.info('Error while retrieving server info for testing')
            return False

        multi_user = bool(server_info.get('multiuser', 0))
        if not multi_user:
            log.info("Server info indicates multi-user support isn't available, WebSocket method not available")
            return False

        return True

    def create_session(self, session_key, state):
        """
        :type session_key: str
        :type state: str

        :rtype: WatchSession or None
        """

        log.debug('Creating a WatchSession for the current media')

        skip = False

        info = PlexMediaServer.get_session(session_key)
        if not info:
            return None

        # Client
        player_section = info.findall('Player')
        if len(player_section):
            player_section = player_section[0]

        client = PlexMediaServer.get_client(player_section.get('machineIdentifier'))

        # Metadata
        metadata = None

        try:
            metadata = PlexMetadata.get(info.get('ratingKey'))

            if metadata:
                metadata = metadata.to_dict()
        except NotImplementedError, e:
            # metadata not supported (music, etc..)
            log.debug('%s, ignoring session' % e.message)
            skip = True

        session = WatchSession.from_section(info, state, metadata, client)
        session.skip = skip
        session.save()

        return session

    def update_session(self, session, view_offset):
        log.debug('Trying to update the current WatchSession (session key: %s)' % session.key)

        video_section = PlexMediaServer.get_session(session.key)
        if not video_section:
            log.warn('Session was not found on media server')
            return False

        log.debug('last item key: %s, current item key: %s' % (session.item_key, video_section.get('ratingKey')))

        if session.item_key != video_section.get('ratingKey'):
            log.debug('Invalid Session: Media changed')
            return False

        session.last_view_offset = view_offset
        session.update_required = False

        return True

    def session_valid(self, session):
        if not session.metadata:
            if session.skip:
                return True

            log.debug('Invalid Session: Missing metadata')
            return False

        if session.metadata.get('duration', 0) <= 0:
            log.debug('Invalid Session: Invalid duration')
            return False

        return True

    def get_session(self, session_key, state, view_offset):
        session = WatchSession.load(session_key)

        if not session:
            session = self.create_session(session_key, state)

            if not session:
                return None

        update_session = False

        # Update session when view offset goes backwards
        if session.last_view_offset and session.last_view_offset > view_offset:
            log.debug('View offset has gone backwards (last: %s, cur: %s)' % (
                session.last_view_offset, view_offset
            ))

            update_session = True

        # Update session on missing metadata + session skip
        if not session.metadata and session.skip:
            update_session = True

        # First try update the session if the media hasn't changed
        # otherwise delete the session
        if update_session and not self.update_session(session, view_offset):
            log.debug('Media changed, deleting the session')
            session.delete()
            return None

        # Delete session if invalid
        if not self.session_valid(session):
            session.delete()
            return None

        if session.skip:
            return None

        if state == 'playing' and session.update_required:
            log.debug('Session update required, updating the session...')

            if not self.update_session(session, view_offset):
                log.debug('Media changed, deleting the session')
                session.delete()
                return None

        return session

    def valid(self, session):
        # Check filters
        if not self.valid_user(session) or\
           not self.valid_client(session) or \
           not self.valid_section(session):
            session.skip = True
            session.save()
            return False

        return True

    def update(self, session_key, state, view_offset):
        # Ignore if scrobbling is disabled
        if not get_pref('scrobble'):
            return

        session = self.get_session(session_key, state, view_offset)
        if not session:
            log.trace('Invalid or ignored session, nothing to do')
            return

        # Ignore sessions flagged as 'skip'
        if session.skip:
            return

        # Validate session (check filters)
        if not self.valid(session):
            return

        media_type = session.get_type()

        # Check if we are scrobbling a known media type
        if not media_type:
            log.info('Playing unknown item, will not be scrobbled: "%s"' % session.get_title())
            session.skip = True
            return

        session.last_view_offset = view_offset

        # Calculate progress
        if not self.update_progress(session, view_offset):
            log.warn('Error while updating session progress, queued session to be updated')
            session.update_required = True
            session.save()
            return

        action = self.get_action(session, state)

        if action:
            self.handle_action(session, media_type, action, state)
        else:
            log.debug(self.status_message(session, state)('Nothing to do this time for %s'))
            session.save()

        if self.handle_state(session, state) or action:
            session.save()
            Dict.Save()

Scrobbler.register(WebSocketScrobbler, weight=10)
