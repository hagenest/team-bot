import logging
from threading import Event

import pickledb
import deltachat
from deltachat import account_hookimpl
from deltachat.capi import lib as dclib
from deltachat.message import _view_type_mapping

from .commands import help_message, set_display_name, set_avatar, start_chat


class SetupPlugin:
    def __init__(self, crew_id):
        self.member_added = Event()
        self.crew_id = crew_id
        self.message_sent = Event()
        self.outgoing_messages = 0

    @account_hookimpl
    def ac_member_added(self, chat: deltachat.Chat, contact, actor, message):
        if chat.id == self.crew_id and chat.num_contacts() == 2:
            self.member_added.set()

    @account_hookimpl
    def ac_message_delivered(self, message: deltachat.Message):
        if not message.is_system_message():
            self.outgoing_messages -= 1
            if self.outgoing_messages < 1:
                self.message_sent.set()


class RelayPlugin:
    def __init__(self, account: deltachat.Account, kvstore: pickledb.PickleDB):
        self.account = account
        self.kvstore = kvstore
        self.crew = account.get_chat_by_id(kvstore.get("crew_id"))
        if not kvstore.get("relays"):
            kvstore.set("relays", list())

    @account_hookimpl
    def ac_incoming_message(self, message: deltachat.Message):
        """This method is called on every incoming message and decides what to do with it."""
        logging.info(
            "New message from %s in chat %s: %s",
            message.get_sender_contact().addr,
            message.chat.get_name(),
            message.text,
        )

        if message.is_system_message():
            logging.debug("This is a system message")
            """:TODO handle chat name changes"""
            return

        if message.chat.id == self.crew.id:
            if message.text.startswith("/"):
                logging.debug(
                    "handling command by %s: %s",
                    message.get_sender_contact().addr,
                    message.text,
                )
                arguments = message.text.split(" ")
                if arguments[0] == "/help":
                    self.reply(message.chat, help_message(), quote=message)
                if arguments[0] == "/set_name":
                    self.reply(
                        message.chat,
                        set_display_name(
                            self.account, message.text.split("/set_name ")[1]
                        ),
                        quote=message,
                    )
                if arguments[0] == "/set_avatar":
                    result = set_avatar(self.account, message, self.crew)
                    self.reply(message.chat, result, quote=message)
                if arguments[0] == "/start_chat":
                    recipients = arguments[1].split(",")
                    title = arguments[2].replace('_', ' ')
                    words = []
                    for i in range(3, len(arguments)):
                        words.append(arguments[i])
                    outside_chat, result = start_chat(
                        self.account,
                        recipients,
                        title,
                        " ".join(words),
                        message.filename if message.filename else "",
                        self.get_message_view_type(message),
                    )
                    if "success" in result:
                        for msg in outside_chat.get_messages():
                            self.forward_to_relay_group(msg)
                    self.reply(message.chat, result, quote=message)
            else:
                logging.debug("Ignoring message, just the crew chatting")

        elif self.is_relay_group(message.chat):
            if message.quote:
                if (
                    message.quote.get_sender_contact()
                    == self.account.get_self_contact()
                ):
                    logging.debug("Forwarding message to outsider")
                    self.forward_to_outside(message)
                else:
                    logging.debug("Ignoring message, just the crew chatting")
            else:
                logging.debug("Ignoring message, just the crew chatting")

        else:
            logging.debug("Forwarding message to relay group")
            self.forward_to_relay_group(message)

    def reply(self, chat: deltachat.Chat, text: str, quote: deltachat.Message = None):
        """Send a reply to a chat, with optional quote."""
        msg = deltachat.Message.new_empty(self.account, view_type="text")
        msg.set_text(text)
        msg.quote = quote
        sent_id = dclib.dc_send_msg(self.account._dc_context, chat.id, msg._dc_msg)
        assert sent_id == msg.id

    def forward_to_outside(self, message: deltachat.Message):
        """forward an answer to an outsider."""
        outside_chat = self.get_outside_chat(message.chat.id)
        if not outside_chat:
            logging.error(
                "Couldn't find the corresponding outside chat for relay group %s",
                message.chat.id,
            )
            return
        outside_chat.send_msg(message)

    def forward_to_relay_group(self, message: deltachat.Message):
        """forward a request to a relay group; create one if it doesn't exist yet."""
        outsider = message.get_sender_contact().addr
        crew_members = self.crew.get_contacts()
        crew_members.remove(self.account.get_self_contact())
        relay_group = self.get_relay_group(message.chat.id)

        if not relay_group:
            group_name = "[%s] %s" % (
                self.account.get_config("addr").split("@")[0],
                message.chat.get_name(),
            )
            logging.info("creating new relay group: '%s'", group_name)
            relay_group = self.account.create_group_chat(
                group_name, crew_members, verified=False
            )
            # relay_group.set_profile_image("assets/avatar.jpg")
            relay_group.send_text(
                "This is the relay group for %s; I'll only forward 'direct replies' to the outside."
                % (message.chat.get_name())
            )
            relay_mappings = self.kvstore.get("relays")
            relay_mappings.append(tuple([message.chat.id, relay_group.id]))
            self.kvstore.set("relays", relay_mappings)

        message.set_override_sender_name(outsider)
        relay_group.send_msg(message)

    def is_relay_group(self, chat: deltachat.Chat) -> bool:
        """Check whether a chat is a relay group."""
        if not chat.get_name().startswith(
            "[%s] " % (self.account.get_config("addr").split("@")[0],)
        ):
            return False  # all relay groups' names begin with a [tag] with the localpart of the teamsbot's address
        if (
            chat.get_messages()[0].get_sender_contact()
            != self.account.get_self_contact()
        ):
            return False  # all relay groups were started by the teamsbot
        if chat.is_protected():
            return False  # relay groups don't need to be protected, so they are not
        for crew_member in self.crew.get_contacts():
            if crew_member not in chat.get_contacts():
                return False  # all crew members have to be in any relay group
        return True

    def get_outside_chat(self, relay_group_id: int) -> deltachat.Chat:
        """Get the corresponding outside chat for the ID of a relay group.

        :param relay_group_id: the chat.id of the relay group
        :return: the outside chat
        """
        relay_mappings = self.kvstore.get("relays")
        for mapping in relay_mappings:
            if mapping[1] == relay_group_id:
                return self.account.get_chat_by_id(mapping[0])
        return None

    def get_relay_group(self, outside_id: int) -> deltachat.Chat:
        """Get the corresponding relay group for the ID of the outside chat.

        :param outside_id: the chat.id of the outside chat
        :return: the relay group
        """
        relay_mappings = self.kvstore.get("relays")
        for mapping in relay_mappings:
            if mapping[0] == outside_id:
                return self.account.get_chat_by_id(mapping[1])
        return None

    def get_message_view_type(self, message: deltachat.Message) -> str:
        """Get the view_type of a Message."""
        for view_name, view_code in _view_type_mapping.items():
            if view_code == message._view_type:
                return view_name
