import multiprocessing
import os
import queue
import threading
import time
import traceback
import uuid

import segment_bytes

from ipi_ecs.core import daemon
import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
import ipi_ecs.db.db_library as db_library
from ipi_ecs.logging.client import LogClient
from ipi_ecs.cli.captive_cli import wait_for

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.exposure_controller import ExposureSettings


class ExposureQueueSubsystem:
	def __init__(self):
		self.__run = True

		c_uuid = uuid.uuid4()

		self.__logger_sock = tcp.TCPClientSocket()
		self.__logger_sock.connect(("127.0.0.1", 11751))
		self.__logger_sock.start()

		self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

		self.__did_config = False
		self.__subsystem = None

		self.__queue: list[bytes] = []
		self.__queue_lock = threading.Lock()

		self.__settings_kv = None
		self.__prepare_event = None
		self.__acquire_automation_event = None
		self.__release_automation_event = None
		self.__run_finalized_kv = None
		self.__lease_owned = False

		self.__save_path = os.path.join(os.environ["EUVL_PATH"], "datasets")
		self.__config_save_queue = queue.Queue()
		self.__config_save_in_progress = False

		self.__pending_queue_advance = False
		self.__pending_lease_release = False
		self.__queue_worker_lock = threading.Lock()
		self.__last_seen_finalized_payload = None

		self.__last_error = b""

		def _on_ready():
			if self.__did_config:
				return

			self.__did_config = True
			sh = self.__client.register_subsystem("Exposure Queue", uuids.UUID_EXPERIMENT_QUEUE_CONTROLLER)
			self.__on_got_subsystem(sh)

		self.__client = client.DDSClient(c_uuid, logger=self.__logger)
		self.__client.when_ready().then(_on_ready)

		self.__config_daemon = daemon.Daemon(exception_handler=self.handle_exception)
		self.__config_daemon.add(self.__config_saver_thread)
		self.__config_daemon.start()

		self.__load_queue()

		self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
		self.__daemon.add(self.__worker_thread)
		self.__daemon.start()

	def handle_exception(self, e: Exception):
		self.__log("Caught exception on daemon thread!", level="ERROR")
		for line in traceback.format_exception(None, e, e.__traceback__):
			for split in line.split("\n"):
				if split:
					self.__log(split, level="ERROR")

	def __log(self, msg, level="INFO", **data):
		if self.__logger is None:
			print(level, msg)
			return

		self.__logger.log(msg, level=level, l_type="CTRL", subsystem="Exposure Queue Controller", **data)

	def __on_run_finalized_update(self, value: bytes):
		try:
			if value is None:
				return

			if self.__last_seen_finalized_payload == value:
				return

			self.__last_seen_finalized_payload = value

			parts = segment_bytes.decode(value)
			if len(parts) != 3:
				return

			r_uuid = uuid.UUID(bytes=parts[0])
			code = parts[1].decode("utf-8", errors="replace")
			reason = parts[2].decode("utf-8", errors="replace")
			self.__log(
				f"Observed run_finalized for run {str(r_uuid)[-8:]} ({code}): {reason}",
				level="INFO",
				event="run_finalized",
			)

			if code == "STOPPED":
				self.__pending_queue_advance = True
			else:
				self.__pending_lease_release = True
				self.__log(
					f"Skipping queue advance for non-stopped finalization code: {code}",
					level="INFO",
					event="run_finalized_skip",
				)
		except Exception as exc:
			self.__log(f"Failed to process run_finalized payload: {exc}", level="ERROR", event="run_finalized")

	def __on_queue_start_event(self, _s_uuid, _param, handle: client._EventHandler._IncomingEventHandle):
		self.__pending_queue_advance = True
		handle.ret(magics.OP_OK + b": queue start requested.")

	def __on_add_to_queue_write(self, _h, _requester, value):
		try:
			b_settings = self.__normalize_item(value)
		except (TypeError, ValueError) as exc:
			return (magics.TRANSOP_STATE_REJ, f"Invalid queued settings: {exc}".encode("utf-8", errors="replace"))

		with self.__queue_lock:
			self.__queue.append(b_settings)
			q_len = len(self.__queue)

		self.__request_queue_save()
		self.__log(f"Added item to queue. Queue length is now {q_len}.", level="INFO", event="queue_add")
		return (magics.TRANSOP_STATE_OK, magics.OP_OK)

	def __on_queue_get(self, _requester):
		with self.__queue_lock:
			payload = segment_bytes.encode(list(self.__queue))
		return (magics.TRANSOP_STATE_OK, payload)

	def __on_queue_set(self, _h, _requester, value):
		try:
			b_queue = self.__decode_queue_bytes(value)
		except (TypeError, ValueError) as exc:
			return (magics.TRANSOP_STATE_REJ, f"Invalid queue payload: {exc}".encode("utf-8", errors="replace"))

		with self.__queue_lock:
			self.__queue = b_queue
			q_len = len(self.__queue)
			if q_len == 0:
				self.__pending_lease_release = True

		self.__request_queue_save()
		self.__log(f"Queue replaced with {q_len} item(s).", level="INFO", event="queue_replace")
		return (magics.TRANSOP_STATE_OK, magics.OP_OK)

	def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
		self.__subsystem = handle

		add_kv = handle.add_kv_handler(b"add_to_queue")
		add_kv.set_type(types.ByteTypeSpecifier())
		add_kv.on_set(self.__on_add_to_queue_write)

		queue_kv = handle.add_kv_handler(b"queue")
		queue_kv.set_type(types.ByteTypeSpecifier())
		queue_kv.on_get(self.__on_queue_get)
		queue_kv.on_set(self.__on_queue_set)

		handle.add_event_handler(b"queue_start_exposure").on_called(self.__on_queue_start_event)

		self.__settings_kv = handle.add_remote_kv(
			uuids.UUID_EXPOSURE_CONTROLLER,
			subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"settings", False, True, True),
		)
		self.__run_finalized_kv = handle.add_remote_kv(
			uuids.UUID_EXPOSURE_CONTROLLER,
			subsystem.KVDescriptor(types.ByteTypeSpecifier(), b"run_finalized", True, True, False),
		)
		self.__run_finalized_kv.on_new_data_received(self.__on_run_finalized_update)

		self.__prepare_event = handle.add_event_provider(b"prepare_exposure")
		self.__acquire_automation_event = handle.add_event_provider(b"acquire_exposure_automation")
		self.__release_automation_event = handle.add_event_provider(b"release_exposure_automation")

	@staticmethod
	def __normalize_item(value) -> bytes:
		if isinstance(value, (bytearray, memoryview)):
			value = bytes(value)

		if not isinstance(value, bytes):
			raise TypeError(f"Queue item must be bytes, got {type(value)}")

		ExposureSettings.decode(value.decode("utf-8"))
		return value

	def __decode_queue_bytes(self, payload) -> list[bytes]:
		if isinstance(payload, (bytearray, memoryview)):
			payload = bytes(payload)

		if payload is None:
			return []

		if isinstance(payload, list):
			items = payload
		else:
			if not isinstance(payload, bytes):
				raise TypeError(f"Queue payload must be bytes or list, got {type(payload)}")
			items = list(segment_bytes.decode(payload))

		out = []
		for item in items:
			out.append(self.__normalize_item(item))

		return out

	def __get_or_create_record(self, library: db_library.Library):
		recs = library.query({"name": "Exposure Queue Save State", "limit": 1})

		if not recs:
			return library.create_entry("Exposure Queue Save State", "Saves the state of the exposure queue")

		return recs[0]

	def __save_queue(self, library: db_library.Library, queue_items: list[bytes]):
		rec = self.__get_or_create_record(library)
		payload = segment_bytes.encode(queue_items)

		res = rec.resource("queue_state.bin", "Queue State", "wb")
		res.write(payload)
		res.close()

	def __load_queue_data(self, library: db_library.Library) -> list[bytes]:
		rec = self.__get_or_create_record(library)

		try:
			res = rec.resource("queue_state.bin", "Queue State", "rb")
			payload = res.read()
			res.close()

			return self.__decode_queue_bytes(payload)
		except (FileNotFoundError, ValueError, TypeError, IndexError):
			res = rec.resource("queue_state.bin", "Queue State", "wb")
			res.write(segment_bytes.encode([]))
			res.close()
			return []

	def __request_saver_command(self, op: str, payload=None, wait: bool = False, timeout: float = 0.0):
		response_queue = queue.Queue(maxsize=1) if wait else None
		self.__config_save_queue.put((op, payload, response_queue))

		if not wait:
			return True, None

		try:
			return response_queue.get(timeout=timeout)
		except queue.Empty:
			return False, b"Timed out waiting for saver thread response."

	def __request_queue_save(self):
		with self.__queue_lock:
			payload = list(self.__queue)

		self.__request_saver_command("save", payload=payload)

	def __load_queue(self):
		ok, payload = self.__request_saver_command("load", wait=True, timeout=5.0)
		if not ok:
			self.__log(f"Failed to load queue from saver thread: {payload}", level="ERROR", event="queue_load")
			return

		with self.__queue_lock:
			self.__queue = list(payload)

		self.__log(f"Loaded {len(self.__queue)} queued item(s).", level="INFO", event="queue_load")

	def __config_saver_thread(self, stop_flag: daemon.StopFlag):
		library = db_library.Library(self.__save_path)
		try:
			while stop_flag.run() and self.__run:
				try:
					op, payload, response_queue = self.__config_save_queue.get(timeout=0.2)
				except queue.Empty:
					continue

				if op == "save":
					queue_payload = payload

					while not self.__config_save_queue.empty():
						try:
							n_op, n_payload, n_response_queue = self.__config_save_queue.get_nowait()
							if n_op == "save":
								queue_payload = n_payload
								continue

							self.__config_save_queue.put((n_op, n_payload, n_response_queue))
							break
						except queue.Empty:
							break

					self.__config_save_in_progress = True
					try:
						self.__save_queue(library, queue_payload)
						if response_queue is not None:
							response_queue.put((True, None))
					except Exception as exc:
						self.__log(f"Failed to save queue: {exc}", level="ERROR", event="queue_save")
						if response_queue is not None:
							response_queue.put((False, str(exc).encode("utf-8", errors="replace")))
					finally:
						self.__config_save_in_progress = False
					continue

				if op == "load":
					try:
						loaded = self.__load_queue_data(library)
						if response_queue is not None:
							response_queue.put((True, loaded))
					except Exception as exc:
						self.__log(f"Failed to load queue: {exc}", level="ERROR", event="queue_load")
						if response_queue is not None:
							response_queue.put((False, str(exc).encode("utf-8", errors="replace")))
					continue

				if op == "close":
					if response_queue is not None:
						response_queue.put((True, None))
					return

				if response_queue is not None:
					response_queue.put((False, b"Unknown saver operation."))
		finally:
			try:
				library.close()
			except Exception:
				pass

	def __wait_event_completion(self, handle: client._InProgressEvent._Handle, target_uuid: uuid.UUID, timeout: float = 30.0):
		begin = time.monotonic()
		while handle.is_in_progress() and (time.monotonic() - begin) < timeout:
			time.sleep(0.1)

		if handle.is_in_progress():
			return False, b"Timed out waiting for prepare_exposure response."

		state = handle.get_state(target_uuid)
		result = handle.get_result(target_uuid)

		if state != client.EVENT_OK:
			if isinstance(result, bytes) and len(result) > 0:
				return False, result
			return False, b"prepare_exposure failed."

		return True, result if isinstance(result, bytes) else magics.OP_OK

	def __set_settings_on_controller(self, b_item: bytes):
		if self.__settings_kv is None:
			return False, b"Exposure settings KV handle is unavailable."

		settings = ExposureSettings.decode(b_item.decode("utf-8"))
		payload = settings.get_dict()

		for key, value in payload.items():
			b_write = segment_bytes.encode([key.encode("utf-8"), str(value).encode("utf-8")])
			awaiter = self.__settings_kv.try_set(b_write, client.KVP_RET_AWAIT)
			if awaiter is None:
				return False, f"Failed to issue settings write for key {key}.".encode("utf-8")

			try:
				ret, state, reason = wait_for(awaiter, timeout=5.0)
			except TimeoutError:
				return False, f"Timed out while writing settings key {key}.".encode("utf-8")

			if state is not None:
				if isinstance(reason, bytes) and len(reason) > 0:
					return False, reason
				return False, f"Settings write rejected for key {key}.".encode("utf-8")

			if isinstance(ret, bytes) and ret not in (magics.OP_OK, b""):
				return False, ret

		return True, magics.OP_OK

	def __ensure_automation_lease(self):
		if self.__lease_owned:
			return True, magics.OP_OK
		if self.__acquire_automation_event is None:
			return False, b"Exposure automation lease provider is unavailable."
		event_handle = self.__acquire_automation_event.call(
			b"Exposure Queue Controller",
			[uuids.UUID_EXPOSURE_CONTROLLER],
		)
		if event_handle is None:
			return False, b"Failed to send exposure automation lease request."
		ok, reason = self.__wait_event_completion(event_handle, uuids.UUID_EXPOSURE_CONTROLLER, timeout=10.0)
		if ok:
			self.__lease_owned = True
		return ok, reason

	def __release_automation_lease(self):
		if not self.__lease_owned:
			return True, magics.OP_OK
		if self.__release_automation_event is None:
			return False, b"Exposure automation release provider is unavailable."
		event_handle = self.__release_automation_event.call(bytes(), [uuids.UUID_EXPOSURE_CONTROLLER])
		if event_handle is None:
			return False, b"Failed to send exposure automation release request."
		ok, reason = self.__wait_event_completion(event_handle, uuids.UUID_EXPOSURE_CONTROLLER, timeout=10.0)
		if ok:
			self.__lease_owned = False
		return ok, reason

	def __start_next_if_available(self):
		with self.__queue_worker_lock:
			with self.__queue_lock:
				if len(self.__queue) == 0:
					self.__last_error = b""
					ok, reason = self.__release_automation_lease()
					if not ok:
						self.__last_error = reason
						self.__log(f"Failed to release queue automation lease: {reason}", level="ERROR", event="queue_lease")
					return
				b_item = self.__queue[0]

			ok, reason = self.__ensure_automation_lease()
			if not ok:
				self.__last_error = reason
				self.__log(f"Failed to acquire queue automation lease: {reason}", level="ERROR", event="queue_lease")
				return

			ok, reason = self.__set_settings_on_controller(b_item)
			if not ok:
				self.__release_automation_lease()
				self.__last_error = reason
				self.__log(f"Failed to apply queued settings: {reason}", level="ERROR", event="queue_apply")
				return

			if self.__prepare_event is None:
				self.__release_automation_lease()
				self.__last_error = b"prepare_exposure provider is unavailable."
				self.__log("prepare_exposure provider is unavailable.", level="ERROR", event="queue_start")
				return

			event_handle = self.__prepare_event.call(bytes(), [uuids.UUID_EXPOSURE_CONTROLLER])
			if event_handle is None:
				self.__release_automation_lease()
				self.__last_error = b"Failed to send prepare_exposure event."
				self.__log("Failed to send prepare_exposure event.", level="ERROR", event="queue_start")
				return

			ok, reason = self.__wait_event_completion(event_handle, uuids.UUID_EXPOSURE_CONTROLLER)
			if not ok:
				self.__release_automation_lease()
				self.__last_error = reason
				self.__log(f"Queued run did not start: {reason}", level="ERROR", event="queue_start")
				return

			with self.__queue_lock:
				if len(self.__queue) > 0 and self.__queue[0] == b_item:
					self.__queue.pop(0)

			self.__request_queue_save()
			self.__last_error = b""
			self.__log("Queued run started successfully.", level="INFO", event="queue_start")

	def __worker_thread(self, stop_flag: daemon.StopFlag):
		while stop_flag.run() and self.__run:
			if self.__pending_lease_release:
				self.__pending_lease_release = False
				ok, reason = self.__release_automation_lease()
				if not ok:
					self.__last_error = reason
					self.__log(f"Failed to release queue automation lease: {reason}", level="ERROR", event="queue_lease")
			if self.__pending_queue_advance:
				self.__pending_queue_advance = False
				self.__start_next_if_available()

			time.sleep(0.05)

	def ok(self):
		return self.__run and self.__client.ok() and self.__daemon.is_ok() and self.__config_daemon.is_ok()

	def close(self):
		self.__run = False
		try:
			self.__release_automation_lease()
		except Exception:
			pass

		begin = time.monotonic()
		while (not self.__config_save_queue.empty() or self.__config_save_in_progress) and (time.monotonic() - begin) < 2.0:
			time.sleep(0.05)

		if self.__daemon is not None:
			self.__daemon.stop()

		if self.__config_daemon is not None:
			self.__config_daemon.stop()

		if self.__client is not None:
			self.__client.close()

		if self.__logger_sock is not None:
			self.__logger_sock.close()


def main(stop_event=None):
	subsystem = ExposureQueueSubsystem()

	try:
		while subsystem.ok() and not (stop_event is not None and stop_event.is_set()):
			time.sleep(1)
	except KeyboardInterrupt:
		pass
	finally:
		subsystem.close()


if __name__ == "__main__":
	main(None)
