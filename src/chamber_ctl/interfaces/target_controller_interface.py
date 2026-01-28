import argparse
import contextlib
import io
import sys
import time
import uuid
from typing import Dict, Iterable, Set

from chamber_ctl.subsystems.target_controller import TargetMotionControllerState, MotionState, TargetMotionProfile, MotionSegment
import mt_events
import segment_bytes

import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
from ipi_ecs.logging.client import LogClient
from ipi_ecs.cli.commands.keyboard_jog import (
    JogWriteClient,
    KeyPoller,
    vector_from_keys,
)

from chamber_ctl.subsystems.uuids import UUID_TARGET_CONTROLLER

class TargetClient:
    def __init__(self):
        self.__run = True

        self.__nd_event = mt_events.Event()
        c_uuid = uuid.uuid4()
        s_uuid = uuid.uuid4()

        self.__logger_sock = tcp.TCPClientSocket()

        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()

        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__did_config = False
        self.__subsystem = None

        self.__current_state = None
        self.__motion_state = None

        self.__status_kv = None

        def _on_ready():
            if self.__did_config:
                return
            
            self.__did_config = True
            sh = self.__client.register_subsystem(f"__cli_{s_uuid}", s_uuid, temporary=True)

            self.__on_got_subsystem(sh)

        #print("Registering subsystem...")
        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(_on_ready)

        self.__on_data = mt_events.Event()

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        self.__subsystem = handle

        self.__status_kv = handle.add_remote_kv(UUID_TARGET_CONTROLLER, subsystem.KVDescriptor(types.VectorTypeSpecifier(types.ByteTypeSpecifier(), 2), b"status", True, True, False))
        self.__status_kv.on_new_data_received(self.__on_status_update)

        self.__profile_kv = handle.add_remote_kv(UUID_TARGET_CONTROLLER, subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"profile", False, True, True))

        self.__start_event_sender = handle.add_event_provider(b"start_target_motion")
        self.__stop_event_sender = handle.add_event_provider(b"stop_target_motion")
        self.__set_start_event_sender = handle.add_event_provider(b"set_target_start")
        self.__home_event_sender = handle.add_event_provider(b"home_target")
        self.__set_position_event_sender = handle.add_event_provider(b"set_target_position")
        self.__set_position_event_sender.set_types(types.FloatTypeSpecifier(), types.ByteTypeSpecifier())

    def __on_status_update(self, n_status: bytes):
        self.__current_state = TargetMotionControllerState.decode(n_status[0])
        self.__motion_state = MotionState.decode(n_status[1])

    def get_state(self) -> TargetMotionControllerState:
        return self.__current_state
    
    def get_motion_state(self) -> MotionState:
        return self.__motion_state

    def get_profile(self) -> TargetMotionProfile:
        if self.__profile_kv is not None:
            profile_data = self.__profile_kv.value

            if profile_data is not None:
                return TargetMotionProfile.decode(profile_data)
            
        return None
    
    def set_profile(self, profile: TargetMotionProfile) -> mt_events.Awaiter:
        if self.__profile_kv is not None:
            return self.__profile_kv.try_set(profile.encode())
        
        return None
    
    def start_motion(self):
        if self.__start_event_sender is not None:
            return self.__start_event_sender.call(bytes(), []).after()
        
        return None
    
    def stop_motion(self):
        if self.__stop_event_sender is not None:
            return self.__stop_event_sender.call(bytes(), []).after()
        
        return None
    
    def set_start_position(self):
        if self.__set_start_event_sender is not None:
            return self.__set_start_event_sender.call(bytes(), []).after()
        
        return None
    
    def set_current_position(self, position: float):
        if self.__set_position_event_sender is not None:
            return self.__set_position_event_sender.call(position, []).after()
        
        return None
    
    def home(self):
        if self.__home_event_sender is not None:
            return self.__home_event_sender.call(bytes(), []).after()
        
        return None

    def ok(self):
        return self.__run and self.__client.ok()

    def close(self):
        self.__client.close()
        self.__logger_sock.close()

        self.__run = False

def wait_for(awaiter: mt_events.Awaiter, timeout: float = 10.0):
    done = False
    r_state, r_reason, r_value = None, None, None

    def on_done(state=None, reason=None, value=None):
        nonlocal done, r_state, r_reason, r_value
        r_reason = reason
        r_state = state
        r_value = value
        
        done = True

    begin = time.monotonic()
    awaiter.then(lambda h: on_done(value=h)).catch(on_done)

    while not done and (time.monotonic() - begin) < timeout:
        time.sleep(0.1)

    if done:
        return r_value, r_state, r_reason
    
    raise TimeoutError("Awaiter did not complete in time.")

def wait_for_event(awaiter: mt_events.Awaiter, timeout: float = 10.0):
    done = False
    r_state, r_reason, r_value = None, None, None

    def on_done(state=None, reason=None, value=None):
        nonlocal done, r_state, r_reason, r_value
        r_reason = reason
        r_state = state
        r_value = value
        
        done = True

    begin = time.monotonic()
    awaiter.then(lambda h: on_done(value=h)).catch(on_done)

    while not done and (time.monotonic() - begin) < timeout:
        time.sleep(0.1)

    if done:
        if r_value is not None:
            r_state = r_value.get_state(UUID_TARGET_CONTROLLER)
            if not r_value.get_result(UUID_TARGET_CONTROLLER).startswith(magics.OP_OK):
                r_reason = r_value.get_result(UUID_TARGET_CONTROLLER)
            
            r_value = r_value.get_result(UUID_TARGET_CONTROLLER)
        
        return r_value, r_state, r_reason
    
    raise TimeoutError("Awaiter did not complete in time.")

class TargetClientCLI:
    def __init__(self, t_client: TargetClient):
        self.__t_client = t_client

        p = argparse.ArgumentParser(description="Target controller CLI interface.")
        p.add_argument("--timeout", type=float, default=10.0, help="Timeout for commands in seconds.")

        sub = p.add_subparsers(dest="cmd", required=True)

        pl = sub.add_parser("help", help="Print this help message.")
        pl.set_defaults(fn=lambda args: p.print_help())

        pl = sub.add_parser("home", help="Home the target controller.")
        pl.set_defaults(fn=self.__home)

        pl = sub.add_parser("goto", help="Move the target controller to a specified position at a specified speed.")
        pl.add_argument("l_pos", type=float, help="Linear position")
        pl.add_argument("r_pos", type=float, help="Radial position")
        pl.add_argument("l_speed", type=float, help="Linear speed")
        pl.add_argument("r_speed", type=float, help="Radial speed")
        pl.set_defaults(fn=self.__goto_position)

        pp = sub.add_parser("get_profile", help="Get the current motion profile.")
        pp.set_defaults(fn=self.__get_profile)

        pm = sub.add_parser("profile", help="Edit the motion profile.")
        pm.set_defaults(fn=self.__make_profile)

        pm = sub.add_parser("start", help="Start the motion.")
        pm.set_defaults(fn=self.__start_motion)

        pm = sub.add_parser("stop", help="Stop the motion.")
        pm.set_defaults(fn=self.__stop_motion)

        p_start = sub.add_parser("set_start", help="Set the current start position on the target controller to the current position.")
        p_start.set_defaults(fn=self.__set_start_position)

        pt = sub.add_parser("set_time", help="Set the current time on the target controller.")
        pt.add_argument("position", type=float, help="Current time on path in seconds")
        pt.set_defaults(fn=self.__set_current_position)

        ps = sub.add_parser("get_state", help="Get the current state of the target controller.")
        ps.set_defaults(fn=self.__get_state)

        self.__parser = p

    def __home(self, args: argparse.Namespace):
        print("Homing target...")
        awaiter = self.__t_client.home()

        r_value, r_state, r_reason = wait_for_event(awaiter, args.timeout)

        if not r_value.startswith(magics.OP_OK):
            print(f"Failed to home target: {r_reason}")

    def __set_start_position(self, args: argparse.Namespace):
        print("Set start position to here...")
        awaiter = self.__t_client.set_start_position()

        r_value, r_state, r_reason = wait_for_event(awaiter, args.timeout)

        if not r_value.startswith(magics.OP_OK):
            print(f"Failed to set target start position: {r_reason}")

    def __start_motion(self, args: argparse.Namespace):
        print("Starting motion...")
        awaiter = self.__t_client.start_motion()
        r_value, r_state, r_reason = wait_for_event(awaiter, args.timeout)

        if not r_value.startswith(magics.OP_OK):
            print(f"Failed to start motion: {r_reason}")

    def __stop_motion(self, args: argparse.Namespace):
        print("Stopping motion...")
        awaiter = self.__t_client.stop_motion()
        r_value, r_state, r_reason = wait_for_event(awaiter, args.timeout)

        if not r_value.startswith(magics.OP_OK):
            print(f"Failed to stop motion: {r_reason}")

    def __goto_position(self, args: argparse.Namespace):
        pass

    def __set_current_position(self, args: argparse.Namespace):
        position = args.position
        print(f"Setting current position to {position}...")
        awaiter = self.__t_client.set_current_position(position)
        r_value, r_state, r_reason = wait_for_event(awaiter, args.timeout)

        print(f"Set current position result: {r_value}, {r_reason}, {r_state}")

        if not r_value.startswith(magics.OP_OK):
            print(f"Failed to set current position: {r_reason}")

    def __get_profile(self, _: argparse.Namespace):
        profile = self.__t_client.get_profile()
        if profile is not None:
            print("Current profile:")
            self.__print_profile(profile)
        else:
            print("No profile set.")

    def __print_profile(self, profile: TargetMotionProfile):
        print(len(profile.get_segments()), "segments:")
        for i, segment in enumerate(profile.get_segments()):
            print(f"  Segment {i}): L Target={segment.lin_target}, R Target={segment.rot_target}, L Speed={segment.lin_velocity}, R Speed={segment.rot_velocity}")

    def __get_state(self, _: argparse.Namespace):
        state = self.__t_client.get_state()
        motion_state = self.__t_client.get_motion_state()
        print(f"Current State: {state}, Motion State: {motion_state}")

    def __make_profile(self, _: argparse.Namespace):
        def __read_seg():
            cmd = input("Enter segment (lin_target rot_target lin_velocity rot_velocity) or 'cancel': ")
            if cmd.strip().lower() == "cancel":
                return None
            parts = cmd.strip().split()
            if len(parts) != 4:
                print("Invalid segment format. Please enter four values.")
                return None
            try:
                lin_target = float(parts[0])
                rot_target = float(parts[1])
                lin_velocity = float(parts[2])
                rot_velocity = float(parts[3])
                segment = MotionSegment(lin_target, rot_target, lin_velocity, rot_velocity)
                return segment
            except ValueError:
                print("Invalid number format. Please enter valid floats.")
                return None
            
        profile = self.__t_client.get_profile()

        if profile is None:
            profile = TargetMotionProfile()

        segments = profile.get_segments()
        
        with contextlib.redirect_stdout(self.__stdout):
            print("Editing motion profile.")
            while True:
                print("Current profile:")
                self.__print_profile(profile)
                cmd = input("Choose 'add', 'remove', 'insert', 'done', or 'cancel': ")
                
                if cmd.strip().lower() == "done":
                    break
                    
                if cmd.strip().lower() == "cancel":
                    return

                if cmd.strip().lower() == "add":
                    segment = __read_seg()
                    if segment is not None:
                        print("Adding segment...")
                        segments.append(segment)

                elif cmd.strip().lower() == "remove":
                    index_str = input("Enter index of segment to remove: ")
                    try:
                        index = int(index_str)
                        if 0 <= index < len(segments):
                            print("Removing segment...")
                            segments.pop(index)
                        else:
                            print("Index out of range.")
                    except ValueError:
                        print("Invalid index format.")
                elif cmd.strip().lower() == "insert":
                    index_str = input("Enter index to insert segment at: ")
                    try:
                        index = int(index_str)
                        if 0 <= index <= len(segments):
                            segment = __read_seg()
                            if segment is not None:
                                print("Inserting segment...")
                                segments.insert(index, segment)
                        else:
                            print("Index out of range.")
                    except ValueError:
                        print("Invalid index format.")


                profile.set_segments(segments)

            awaiter = self.__t_client.set_profile(profile)
            v, s, r = wait_for(awaiter, 10.0)

            if v == magics.OP_OK:
                print("Profile set successfully.")
            else:
                print(f"Failed to set profile: {r}")

    def parse_and_execute(self, argstr: Iterable[str]) -> str:
        captured_output = io.StringIO()
        captured_output_stderr = io.StringIO()

        self.__stdout = sys.stdout
        self.__stderr = sys.stderr

        self.__out_stdout = captured_output
        self.__out_stderr = captured_output_stderr

        with contextlib.redirect_stdout(captured_output):
            with contextlib.redirect_stderr(captured_output_stderr):
                try:
                    m_args = self.__parser.parse_args(argstr)
                    m_args.fn(m_args)
                except SystemExit as e:
                    print(f"Argument parsing failed: {e}")
                except Exception as e:
                    print(f"Error executing command: {e}")
                    raise

        return captured_output.getvalue() + captured_output_stderr.getvalue()

def main(args: argparse.Namespace):
    m_client = TargetClient()
    m_cli = TargetClientCLI(m_client)

    try:
        while m_client.ok():
            commands = input("TargetClient> ")
            if commands.strip().lower() in ("exit", "quit"):
                print("Exiting...")
                break
            output = m_cli.parse_and_execute(commands.strip().split())
            if output.strip() != "":
                print("TargetClient> ", output, end="", sep="")

    except KeyboardInterrupt:
        pass
    finally:
        print("Exiting...")
        m_client.close()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Target controller CLI interface.")
    args = parser.parse_args()

    sys.exit(main(args))
