import asyncio

import ast
import time as ttime
from datetime import datetime
import pprint
import sys
import argparse

import bluesky_queueserver
from .comms import ZMQCommSendAsync

import logging

logger = logging.getLogger(__name__)

qserver_version = bluesky_queueserver.__version__


def get_supported_commands():
    """
    Get the dictionary that maps command names supported by the cli tool to
    the command names in RE Manager API.

    Returns
    -------
    dict(str, str)
        Dictionary that maps supported commands to commands from RE Manager API.
    """
    command_dict = {
        "ping": "",
        "status": "status",
        "plans_allowed": "plans_allowed",
        "devices_allowed": "devices_allowed",
        "history_get": "history_get",
        "history_clear": "history_clear",
        "environment_open": "environment_open",
        "environment_close": "environment_close",
        "environment_destroy": "environment_destroy",
        "queue_get": "queue_get",
        "queue_plan_add": "queue_plan_add",
        "queue_plan_get": "queue_plan_get",
        "queue_plan_remove": "queue_plan_remove",
        "queue_clear": "queue_clear",
        "queue_start": "queue_start",
        "queue_stop": "queue_stop",
        "queue_stop_cancel": "queue_stop_cancel",
        "re_pause": "re_pause",
        "re_resume": "re_resume",
        "re_stop": "re_stop",
        "re_abort": "re_abort",
        "re_halt": "re_halt",
        "manager_stop": "manager_stop",
        "manager_kill": "manager_kill",
    }
    return command_dict


def create_msg(command, params=None):
    # This function may transform human-friendly command names to API names
    params = params or []

    command_dict = get_supported_commands()
    try:
        command = command_dict[command]
        # Present value in the proper format. This will change as the format is changed.
        if command == "queue_plan_add":
            if (len(params) == 1) and isinstance(params[0], dict):
                prms = {"plan": params[0]}  # Value is dict
            elif len(params) == 2:
                plan_found, pos_found = False, False
                prms = {}
                for n in range(2):
                    if isinstance(params[n], dict):
                        prms.update({"plan": params[n]})
                        plan_found = True
                    else:
                        prms.update({"pos": params[n]})
                        pos_found = True
                if not plan_found or not pos_found:
                    raise ValueError("Invalid set of method arguments: '%s'", pprint.pformat(params))
            else:
                raise ValueError("Invalid number of method arguments: '%s'", pprint.pformat(params))
            prms["user"] = "qserver-cli"
            prms["user_group"] = "root"

        elif command in ("queue_plan_remove", "queue_plan_get"):
            if 0 <= len(params) <= 1:
                prms = {"pos": params[0]} if len(params) else {}
            else:
                raise ValueError("Invalid number of method arguments: '%s'", pprint.pformat(params))

        elif command in ("plans_allowed", "devices_allowed"):
            prms = {"user_group": "root"}

        else:
            if 0 <= len(params) <= 1:
                prms = {"option": params[0]} if len(params) else {}  # Value is str
            else:
                raise ValueError("Invalid number of method arguments: '%s'", pprint.pformat(params))

        return command, prms

    except KeyError:
        raise ValueError(f"Command '{command}' is not supported.")


def single_zmq_request(zmq_server_address, method, params):

    msg_received = None

    async def send_request(method, params):
        nonlocal msg_received
        zmq_to_manager = ZMQCommSendAsync(zmq_server_address=zmq_server_address)
        msg_received = await zmq_to_manager.send_message(method=method, params=params)
        del zmq_to_manager  # This will close the socket

    try:
        method, params_out = create_msg(method, params)
        asyncio.run(send_request(method, params_out))

        msg = msg_received
        msg_err = ""
    except Exception as ex:
        msg = None
        msg_err = str(ex)

    if msg_err:
        logger.warning("Communication with RE Manager failed: %s", str(msg_err))

    return msg, msg_err


def qserver():

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("bluesky_queueserver").setLevel("CRITICAL")

    supported_commands = list(get_supported_commands().keys())
    # Add the command 'monitor' to the list. This command is not sent to RE Manager.
    supported_commands = ["monitor"] + supported_commands

    parser = argparse.ArgumentParser(
        description="Command-line tool for communicating with RE Monitor.",
        epilog=f"Bluesky-QServer version {qserver_version}.",
    )
    parser.add_argument(
        "--command",
        "-c",
        dest="command",
        action="store",
        required=True,
        help=f"Command sent to the server. Supported commands: {supported_commands}.",
    )
    parser.add_argument(
        "--parameters",
        "-p",
        nargs="*",
        dest="params",
        action="store",
        default=None,
        help="Parameters that are sent with the command. Currently the parameters "
        "must be represented as a string that contains a python dictionary.",
    )
    parser.add_argument(
        "--address",
        "-a",
        dest="address",
        action="store",
        default=None,
        help="Address of the server (e.g. 'tcp://localhost:5555', quoted string)",
    )

    args = parser.parse_args()

    command, params = args.command, args.params
    params = params or []

    if command not in supported_commands:
        print(
            f"Command '{command}' is not supported. Please enter a valid command.\n"
            f"Call 'qserver' with the option '-h' to see full list of supported commands."
        )
        sys.exit(1)

    # 'params' is a string representing a python dictionary. We need to convert it into a dictionary.
    #   Also don't evaluate the expression that is a non-quoted string with alphanumeric characters.
    for n in range(len(params)):
        if params[n] is not None:
            try:
                params[n] = ast.literal_eval(params[n])
            except Exception as ex:
                # Failures to parse are OK (sometimes expected) unless the parameter is a dictionary.
                # TODO: probably it's a good idea to check if it is a list. (List are not used
                #     as parameter values at this point.)
                if ("{" in params[n]) or ("}" in params[n]):
                    print(
                        f"Failed to parse parameter string {params[n]}: {str(ex)}. "
                        f"The parameters must represent a valid Python dictionary"
                    )
                    sys.exit(1)

    # 'ping' command will be sent to RE Manager periodically if 'monitor' command is entered
    monitor_on = command == "monitor"
    if monitor_on:
        command = "ping"
        print("Running QSever monitor. Press Ctrl-C to exit ...")

    try:
        while True:
            msg, msg_err = single_zmq_request(args.address, command, params)

            now = datetime.now()
            current_time = now.strftime("%H:%M:%S")

            if not msg_err:
                print(f"{current_time} - MESSAGE: {pprint.pformat(msg)}")
                if isinstance(msg, dict) and ("success" in msg) and (msg["success"] is False):
                    exit_code = 2
                else:
                    exit_code = None
            else:
                print(f"{current_time} - ERROR: {msg_err}")
                exit_code = 3

            if not monitor_on:
                break
            ttime.sleep(1)
    except Exception as ex:
        logger.exception("Exception occurred: %s.", str(ex))
        exit_code = 4
    except KeyboardInterrupt:
        print("\nThe program was manually stopped.")
        exit_code = None

    # Note: exit codes are arbitrarily selected. None translates to 0.
    return exit_code
