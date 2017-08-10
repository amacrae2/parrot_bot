from parrot_bot import BOT_TOKEN, get_user_name
from slackclient import SlackClient
import argparse


VALID_CHANNEL_TYPES = ['channels', 'im', 'mpim']
NAME_TP_PATH = {
    'channels': 'channels',
    'im': 'ims',
    'mpim': 'groups'
}
NAME_TO_FX = {
    'channels': lambda x, sc: x.get('name'),
    'im': lambda x, sc: get_user_name(sc, x.get('user')),
    'mpim': lambda x, sc: x.get('name')
}


def print_channels(channel_type):
    """
    print to console a list of channels based on channel type
    """
    sc = SlackClient(BOT_TOKEN)
    channels = [{"#" + str(NAME_TO_FX.get(channel_type)(x, sc)): x.get('id')} for x in sc.api_call("{}.list".format(channel_type)).get(NAME_TP_PATH.get(channel_type))]
    for channel in channels:
        print channel


if __name__ == "__main__":
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--channel_type', '-ch', choices=VALID_CHANNEL_TYPES, default=VALID_CHANNEL_TYPES[0],
                        help="type of channels to list. Choices are {}".format(VALID_CHANNEL_TYPES))
    args = parser.parse_args()
    print_channels(args.channel_type)