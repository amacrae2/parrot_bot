import json
import markovify
import re
import time
import random
import websocket
import logging

from slackclient import SlackClient


BOT_TOKEN = "insert bot token here"
GROUP_TOKEN = "insert group token here"

MESSAGE_QUERY = "from:@{} in:{}"
MESSAGE_PAGE_SIZE = 100
DEBUG = True
NUM_RETRIES = 25

HOME_CHANNEL = "insert channel id of home channel for bot"
STARTUP_CHANNEL = "insert channel id of startup channel for bot"

REPLACE_CHARS = "[]'()"
USER_TO_START_AT = ""  # if running power up all, which user name to start at alphabetically

COMMANDS_MESSAGE = """
Try `parrot me` to see what a parrot version of you might say. \n
>Other parameters - parrot [me/random/user_name(without the @)] [number 1-10]\n
Try `power up me` to give the parrot more slack message data to mimic you with. \n
>Other parameters - power up [me/user_name(without the @)/all]
_An emoji response means I saw your command._
"""

SUPPRESS_AT_CHANNELS = True
SUPPRESS_AT_PERSON = True
SUPPRESS_INDIVIDUAL_USER_NAMES = True

USER_NAMES_TO_SUPPRESS = {
    'first.last': 'alias'
}

REPLACEMENT_NAMES = {
    'alias': 'first.last'
}


# setup a logger
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s :: %(levelname)s :: %(name)s :: %(message)s')


def _load_db(name):
    """
    Reads 'database' from a JSON file on disk.
    Returns a dictionary keyed by unique message permalinks.
    """

    try:
        with open('message_db_{}.json'.format(name), 'r') as json_file:
            messages = json.loads(json_file.read())
    except IOError:
        with open('message_db_{}.json'.format(name), 'w') as json_file:
            json_file.write('{}')
        messages = {}

    return messages


def _store_db(obj, name):
    """
    Takes a dictionary keyed by unique message permalinks and writes it to the JSON 'database' on
    disk.
    """

    with open('message_db_{}.json'.format(name), 'w') as json_file:
        json_file.write(json.dumps(obj))

    return True


def _query_messages(client, name, channel, page=1):
    """
    Convenience method for querying messages from Slack API.
    """
    log.info("requesting page {}".format(page))
    # sometimes have inconsistent ValueError with some messages of a certain type
    max_retires = 5
    for i in xrange(1, max_retires+1):
        try:
            result = client.api_call('search.messages', query=MESSAGE_QUERY.format(name, channel), count=MESSAGE_PAGE_SIZE, page=page)
            return result
        except ValueError:
            if i >= max_retires:
                raise


def _add_messages(message_db, new_messages):
    """
    Search through an API response and add all messages to the 'database' dictionary.
    Returns updated dictionary.
    """
    try:
        for match in new_messages['messages']['matches']:
            message_db[match['permalink']] = match['text']
    except KeyError:  # sometimes get a KeyError here but it is inconsistent
        log.debug(new_messages)
    return message_db


def _get_channels(sc):
    """
    get list of public channels
    """
    return ["#" + str(x.get('name')) for x in sc.api_call("channels.list").get('channels')]


def _get_users_names_list(sc):
    """
    get list of users names
    """
    return [x.get('name') for x in sc.api_call("users.list").get('members')]


def handle_bad_chars(messages_dict):
    """
    get all messages, build a giant text corpus
    """
    messages = messages_dict.values()
    for ch in REPLACE_CHARS:
        messages = [x.replace(ch, '') for x in messages]
    return messages


def build_text_model(sc, channel, name, tries=0):
    """
    Read the latest 'database' off disk and build a new markov chain generator model.
    Returns TextModel.
    """
    log.info("Building new model...")

    messages_dict = _load_db(name)

    # Sometimes messages have issues with bad characters if the message list is too short or something - not clear
    messages = messages_dict.values() if tries == 0 else handle_bad_chars(messages_dict)

    try:
        markovify.Text(" ".join(messages), state_size=2)
    except IndexError:
        for message in messages:
            try:
                markovify.Text(message, state_size=2)
            except IndexError:
                log.debug("issue with message - {}".format(message))
                for part in [message[i:i + 3] for i in xrange(0, len(message))]:
                    try:
                        markovify.Text(part, state_size=2)
                    except IndexError:
                        log.debug("issue on part of message - {}".format(part))
                        if tries <= 2:
                            sc.rtm_send_message(HOME_CHANNEL, "hit a snag on `{}` from ```{}``` - trying again".format(part, message))
                            return build_text_model(sc, channel, name, tries=tries+1)
                        else:
                            sc.rtm_send_message(channel, "having trouble with messages from user {}".format(name))
                            return markovify.Text("", state_size=2)
    return markovify.Text(" ".join(messages), state_size=2)


def format_message(original):
    """
    Do any formatting necessary to markov chains before relaying to Slack.
    """
    if original is None:
        return

    # Clear <> from urls
    cleaned_message = re.sub(r'<(htt.*)>', '\1', original)
    if SUPPRESS_AT_CHANNELS:
        cleaned_message = re.sub('<!channel>', '*at-channel*', cleaned_message)
        cleaned_message = re.sub('<!everyone>', '*at-everyone*', cleaned_message)
        cleaned_message = re.sub('<!here\|@here>', '*at-here*', cleaned_message)
        cleaned_message = re.sub('<!here>', '*at-here*', cleaned_message)
    if SUPPRESS_AT_PERSON:
        cleaned_message = re.sub('<@U.*>:', '', cleaned_message)
        cleaned_message = re.sub('<@U.*>', '', cleaned_message)
    if SUPPRESS_INDIVIDUAL_USER_NAMES:
        for name in USER_NAMES_TO_SUPPRESS:
            cleaned_message = re.sub(name, USER_NAMES_TO_SUPPRESS.get(name), cleaned_message)
    return cleaned_message


def update_corpus(sc, req_channel, name):
    """
    Queries for new messages and adds them to the 'database' object if new ones are found.
    Reports back to the channel where the update was requested on status.
    """

    sc.rtm_send_message(req_channel, "Leveling up {}... this could take a few minutes".format(name))

    # Messages will get queried by a different auth token
    # So we'll temporarily instantiate a new client with that token
    group_sc = SlackClient(GROUP_TOKEN)

    # Load the current database
    messages_db = _load_db(name)
    starting_count = len(messages_db.keys())

    for channel in _get_channels(sc):
        log.info("updating messages from {} for {} using {}".format(channel, name, MESSAGE_QUERY.format(name, channel)))

        # Get first page of messages
        new_messages = _query_messages(group_sc, name, channel)
        try:
            total_pages = new_messages['messages']['paging']['pages']
        except KeyError:  # sometimes get a KeyError here but it is inconsistent
            log.debug(new_messages)
            # TODO handle this better

        # store new messages
        messages_db = _add_messages(messages_db, new_messages)

        # If any subsequent pages are present, get those too
        if total_pages > 1:
            for page in range(2, total_pages + 1):
                new_messages = _query_messages(group_sc, name, channel, page=page)
                messages_db = _add_messages(messages_db, new_messages)

    # See if any new keys were added
    final_count = len(messages_db.keys())
    new_message_count = final_count - starting_count
    sc.rtm_connect()  # reconnect in case it took too long

    # If the count went up, save the new 'database' to disk, report the stats.
    if final_count > starting_count:
        # Write to disk since there is new data.
        _store_db(messages_db, name)
        sc.rtm_send_message(req_channel, "I have been imbued with the power of {} new messages for {}!".format(
            new_message_count, name
        ))
    else:
        sc.rtm_send_message(req_channel, "No new messages found for {} :(".format(name))

    changes = "Start: {}, Final: {}, New: {}".format(starting_count, final_count, new_message_count)
    log.info(changes)
    sc.rtm_send_message(req_channel, changes)

    # Make sure we close any sockets to the other group.
    del group_sc

    return new_message_count


def acknowledge(channel, sc, ts):
    """
    add an emoji to acknowledge message
    """
    sc.api_call(
        "reactions.add",
        channel=channel,
        name=random.choice(sc.api_call("emoji.list").get('emoji').keys()),
        timestamp=ts
    )


def get_channel(sc, channel):
    """
    get name of channel based on id
    """
    return "#{}".format(sc.api_call("channels.info", channel=channel).get('channel', {}).get('name'))


def get_user_name(sc, user):
    """
    get name of user based on id
    """
    return sc.api_call("users.info", user=user).get('user', {}).get('name')


def extract_name_to_parrot(message, sc, user, channel, position=1):
    """
    extract user name to parrot based on message
    """
    rand = False
    try:
        name = message.lower().split()[position]
        log.info("parrot me request from {} in channel {}".format(get_user_name(sc, user), get_channel(sc, channel)))
        if name == 'random':
            name = random.choice([x.get('name') for x in sc.api_call("users.list").get('members')])
            rand = True
        elif name in REPLACEMENT_NAMES:
            name = REPLACEMENT_NAMES.get(name)
        name = name if name != 'me' else get_user_name(sc, user)
    except IndexError:
        name = random.choice([x.get('name') for x in sc.api_call("users.list").get('members')])
        rand = True
    return name, rand


def extract_count_of_parrot_messages(message, position=2):
    """
    extract number of times to parrot the user based om the message
    """
    count = 1
    try:
        count = min(int(message.lower().split()[position]), 10)
    except (ValueError, IndexError):
        pass
    return count


def send_parrot_messages(channel, count, name, rand, sc):
    """
    construct and send a parrot message to slack
    """
    # build the text model
    model = build_text_model(sc, channel, name)
    i = 1
    while i <= count:
        try:
            markov_chain = model.make_sentence().encode('utf-8')
        except AttributeError:
            log.warning("Not enough messages to form parrot response")
            if rand:
                name = random.choice([x.get('name') for x in sc.api_call("users.list").get('members')])
                model = build_text_model(sc, channel, name)
                continue
            else:
                sc.rtm_send_message(channel, "Not enough messages to form parrot response for {}".format(name))
                if i == 1:
                    break
        sentence = "{} - {}".format(markov_chain, name) if rand else markov_chain
        sc.rtm_send_message(channel, format_message(sentence))
        i += 1
        if rand:
            name = random.choice([x.get('name') for x in sc.api_call("users.list").get('members')])
            model = build_text_model(sc, channel, name)


def main():
    """
    Startup logic and the main application loop to monitor Slack events.
    """

    # Create the slackclient instance
    sc = SlackClient(BOT_TOKEN)

    # Connect to slack
    if not sc.rtm_connect():
        raise Exception("Couldn't connect to slack.")

    # Where the magic happens
    sc.rtm_send_message(STARTUP_CHANNEL, "waking up... _(type `parrot commands` to see list of commands)_")

    for i in range(NUM_RETRIES):
        try:
            while True:
                # Examine latest events
                for slack_event in sc.rtm_read():

                    # Disregard events that are not messages
                    if not slack_event.get('type') == "message":
                        continue

                    message = slack_event.get("text")
                    user = slack_event.get("user")
                    channel = slack_event.get("channel")
                    ts = slack_event.get('ts')  # timestamp

                    if not message or not user:
                        continue

                    ######
                    # Commands we're listening for.
                    ######

                    if "parrot commands" == message.lower():
                        acknowledge(channel, sc, ts)
                        sc.rtm_send_message(channel, COMMANDS_MESSAGE)

                    elif "parrot" == message.lower().split()[0]:
                        acknowledge(channel, sc, ts)
                        name, rand = extract_name_to_parrot(message, sc, user, channel)
                        count = extract_count_of_parrot_messages(message)
                        send_parrot_messages(channel, count, name, rand, sc)

                    elif "power up" == " ".join(message.lower().split()[:2]):
                        acknowledge(channel, sc, ts)
                        name = message.lower().split()[2]
                        name = name if name != 'me' else get_user_name(sc, user)
                        if name == 'all':
                            for name in _get_users_names_list(sc):
                                if name >= USER_TO_START_AT:
                                    if update_corpus(sc, channel, name) > 0:
                                        build_text_model(sc, channel, name)
                        else:
                            # Fetch new messages.  If new ones are found, rebuild the text model
                            if update_corpus(sc, channel, name) > 0:
                                build_text_model(sc, channel, name)

                # Sleep for half a second
                time.sleep(0.5)
        except (ValueError, KeyError, IndexError) as e:
            log.exception("uh oh, something went wrong")
            sc.rtm_send_message(channel, "uh oh, something went wrong - try again...")
            pass  # TODO debug these Errors better
        except websocket._exceptions.WebSocketConnectionClosedException:
            sc.rtm_connect()
        except:
            sc.rtm_send_message(STARTUP_CHANNEL, "going to sleep...")
            raise
    sc.rtm_send_message(STARTUP_CHANNEL, "going to sleep...")


if __name__ == '__main__':
    main()
