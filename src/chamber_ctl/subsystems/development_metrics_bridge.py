import json
import math
import os
import queue
import time
import traceback
import uuid

from ipi_ecs.core import daemon
import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.types as types
from ipi_ecs.dds.magics import OP_OK
from ipi_ecs.logging.client import LogClient
from ipi_ecs.subsystems.experiment_controller import ExperimentReader

from chamber_ctl.subsystems import uuids
from chamber_ctl.subsystems.development_metrics import DevelopmentMetrics


class DevelopmentMetricsBridgeSubsystem:
    def __init__(self):
        self.__run = True
        self.__did_config = False

        self.__data_path = os.path.join(os.environ["EUVL_PATH"], "datasets")
        self.__metrics = DevelopmentMetrics(self.__data_path)
        self.__db_queue = queue.Queue()

        c_uuid = uuid.uuid4()
        self.__logger_sock = tcp.TCPClientSocket()
        self.__logger_sock.connect(("127.0.0.1", 11751))
        self.__logger_sock.start()
        self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)

        self.__client = client.DDSClient(c_uuid, logger=self.__logger)
        self.__client.when_ready().then(self.__on_ready)

        self.__daemon = daemon.Daemon(exception_handler=self.handle_exception)
        self.__daemon.add(self.__db_worker_thread)
        self.__daemon.start()

    def __log(self, msg: str, level: str = "INFO", **data):
        if self.__logger is None:
            print(level, msg)
            return
        self.__logger.log(msg, level=level, l_type="SW", subsystem="Development Metrics Controller", **data)

    def handle_exception(self, e: Exception):
        self.__log("Caught exception on daemon thread!", level="ERROR")
        for line in traceback.format_exception(None, e, e.__traceback__):
            for split in line.split("\n"):
                if split:
                    self.__log(split, level="ERROR")

    def __on_ready(self, _=None):
        if self.__did_config:
            return
        self.__did_config = True

        handle = self.__client.register_subsystem(
            "Development Metrics Controller",
            uuids.UUID_DEVELOPMENT_METRICS_CONTROLLER,
        )
        self.__on_got_subsystem(handle)

    def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
        query_handler = handle.add_event_handler(b"query_exposures")
        query_handler.on_called(self.__on_query_exposures)
        query_handler.set_types(types.ByteTypeSpecifier(), types.ByteTypeSpecifier())

        read_handler = handle.add_event_handler(b"read_exposure")
        read_handler.on_called(self.__on_read_exposure)
        read_handler.set_types(types.ByteTypeSpecifier(), types.ByteTypeSpecifier())

        save_handler = handle.add_event_handler(b"save_measurements")
        save_handler.on_called(self.__on_save_measurements)
        save_handler.set_types(types.ByteTypeSpecifier(), types.ByteTypeSpecifier())

        self.__log("Development metrics DDS endpoints configured.")

    @staticmethod
    def __as_json_bytes(payload: dict) -> bytes:
        return json.dumps(payload).encode("utf-8")

    @staticmethod
    def __decode_json_payload(payload) -> dict:
        if payload is None:
            return {}
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = str(payload)
        text = text.strip()
        if not text:
            return {}
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("Payload must be a JSON object.")
        return obj

    @staticmethod
    def __to_float(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def __is_finite_number(value) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def __start_end_for_date_str(date_str: str) -> tuple[float, float]:
        ts_min = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
        return ts_min, ts_min + 86400.0

    def __respond_ok(self, handle, data: dict):
        handle.ret(OP_OK + self.__as_json_bytes({"ok": True, **data}))

    def __respond_err(self, handle, error: str):
        handle.ret(OP_OK + self.__as_json_bytes({"ok": False, "error": error}))

    def __db_worker_thread(self, stop_flag: daemon.StopFlag):
        # Keep DB connections thread-affine by constructing ExperimentReader in this worker.
        exp_reader = ExperimentReader(self.__data_path, "exposure")
        while stop_flag.run() and self.__run:
            try:
                fn, result_queue = self.__db_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                result_queue.put(("ok", fn(exp_reader)))
            except Exception as e:
                result_queue.put(("err", e))

    def __run_db_call(self, fn):
        result_queue = queue.Queue()
        self.__db_queue.put((fn, result_queue))
        status, result = result_queue.get()
        if status == "err":
            raise result
        return result

    def __query_exposures_for_date(self, date_str: str) -> list[dict]:
        return self.__run_db_call(lambda exp_reader: self.__query_exposures_for_date_with_reader(exp_reader, date_str))

    def __query_exposures_for_date_with_reader(self, exp_reader: ExperimentReader, date_str: str) -> list[dict]:
        ts_min, ts_max = self.__start_end_for_date_str(date_str)
        runs = exp_reader.query({"created_min": ts_min, "created_max": ts_max})

        out = []
        for record in runs:
            meta = record.get_metadata() or {}
            created_at = self.__to_float(meta.get("created_at"))
            if created_at is None:
                continue

            state = record.get_state()
            settings = state.get_settings().get_dict() if state is not None else {}
            tags = record.get_tags() or {}

            out.append(
                {
                    "created_at": created_at,
                    "uuid": str(state.get_uuid()) if state is not None else "",
                    "name": record.get_name() or "",
                    "description": record.get_description() or "",
                    "recorded_dose": self.__to_float(tags.get("dose")),
                    "recorded_runtime": self.__to_float(tags.get("runtime")),
                    "target_dose": self.__to_float(settings.get("target_dose")),
                    "target_time": self.__to_float(settings.get("target_time")),
                }
            )

        out.sort(key=lambda item: item.get("created_at", 0.0), reverse=True)
        return out

    @staticmethod
    def __metrics_to_rows(data: dict) -> list[dict]:
        measurements = data.get("measurements")
        if isinstance(measurements, list):
            out = []
            for m in measurements:
                if not isinstance(m, dict):
                    continue
                spot = str(m.get("spot_type", "")).strip().lower()
                if spot not in ("exposed", "blank"):
                    continue
                try:
                    thickness = float(m.get("thickness_nm"))
                    gof = float(m.get("goodness_of_fit"))
                except (TypeError, ValueError):
                    continue
                out.append({"spot_type": spot, "thickness_nm": thickness, "goodness_of_fit": gof})
            return out

        exposed = data.get("exposed_area_thickness_nm", []) or []
        blank = data.get("blank_area_thickness_nm", []) or []
        gof = data.get("goodness_of_fit", []) or []

        exposed_vals = [float(v) for v in exposed if DevelopmentMetricsBridgeSubsystem.__is_finite_number(v)]
        blank_vals = [float(v) for v in blank if DevelopmentMetricsBridgeSubsystem.__is_finite_number(v)]

        out = []

        # New format: gof values are [all exposed gof] + [all blank gof].
        if len(gof) == (len(exposed_vals) + len(blank_vals)):
            exposed_gof = gof[:len(exposed_vals)]
            blank_gof = gof[len(exposed_vals):len(exposed_vals) + len(blank_vals)]

            for i, thickness in enumerate(exposed_vals):
                g_val = exposed_gof[i] if i < len(exposed_gof) else None
                gof_val = float(g_val) if DevelopmentMetricsBridgeSubsystem.__is_finite_number(g_val) else 0.0
                out.append({"spot_type": "exposed", "thickness_nm": thickness, "goodness_of_fit": gof_val})

            for i, thickness in enumerate(blank_vals):
                g_val = blank_gof[i] if i < len(blank_gof) else None
                gof_val = float(g_val) if DevelopmentMetricsBridgeSubsystem.__is_finite_number(g_val) else 0.0
                out.append({"spot_type": "blank", "thickness_nm": thickness, "goodness_of_fit": gof_val})

            return out

        # Backward compatibility for legacy aligned format where arrays had matching length.
        n = max(len(exposed), len(blank), len(gof))
        for i in range(n):
            e_val = exposed[i] if i < len(exposed) else None
            b_val = blank[i] if i < len(blank) else None
            g_val = gof[i] if i < len(gof) else None

            spot = None
            thickness = None
            if DevelopmentMetricsBridgeSubsystem.__is_finite_number(e_val):
                spot = "exposed"
                thickness = float(e_val)
            elif DevelopmentMetricsBridgeSubsystem.__is_finite_number(b_val):
                spot = "blank"
                thickness = float(b_val)

            if spot is None:
                continue

            gof_val = float(g_val) if DevelopmentMetricsBridgeSubsystem.__is_finite_number(g_val) else 0.0
            out.append({"spot_type": spot, "thickness_nm": thickness, "goodness_of_fit": gof_val})

        return out

    @staticmethod
    def __rows_to_metrics(rows: list[dict]) -> tuple[list[float], list[float], list[float]]:
        exposed = []
        blank = []
        gof_exposed = []
        gof_blank = []

        for m in rows:
            spot = str(m.get("spot_type", "")).strip().lower()
            thickness = float(m.get("thickness_nm"))
            fit = float(m.get("goodness_of_fit"))

            if spot == "exposed":
                exposed.append(thickness)
                gof_exposed.append(fit)
            elif spot == "blank":
                blank.append(thickness)
                gof_blank.append(fit)
            else:
                raise ValueError(f"Invalid spot_type '{spot}'.")

        gof = gof_exposed + gof_blank

        return exposed, blank, gof

    def __on_query_exposures(self, _s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        try:
            payload = self.__decode_json_payload(param)
            date_str = str(payload.get("date", time.strftime("%Y-%m-%d"))).strip()
            exposures = self.__query_exposures_for_date(date_str)
            self.__respond_ok(handle, {"date": date_str, "exposures": exposures})
        except Exception as e:
            self.__respond_err(handle, str(e))

    def __on_read_exposure(self, _s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        try:
            payload = self.__decode_json_payload(param)
            exposure_uuid = uuid.UUID(str(payload.get("exposure_uuid", "")).strip())
        except Exception as e:
            self.__respond_err(handle, f"Invalid read_exposure payload: {e}")
            return

        try:
            data = self.__metrics.read_ellipsometry_data(exposure_uuid)
            rows = self.__metrics_to_rows(data if isinstance(data, dict) else {})
            self.__respond_ok(handle, {"exposure_uuid": str(exposure_uuid), "measurements": rows})
        except FileNotFoundError:
            self.__respond_ok(handle, {"exposure_uuid": str(exposure_uuid), "measurements": []})
        except Exception as e:
            msg = str(e).lower()
            if "ellipsometry.json" in msg and (
                "no such file" in msg or "not found" in msg or "does not exist" in msg
            ):
                self.__respond_ok(handle, {"exposure_uuid": str(exposure_uuid), "measurements": []})
                return
            self.__respond_err(handle, str(e))

    def __on_save_measurements(self, _s_uuid, param, handle: client._EventHandler._IncomingEventHandle):
        try:
            payload = self.__decode_json_payload(param)
            exposure_uuid = uuid.UUID(str(payload.get("exposure_uuid", "")).strip())
            rows = payload.get("measurements", [])
            if not isinstance(rows, list):
                raise ValueError("measurements must be a list")

            normalized = []
            for item in rows:
                if not isinstance(item, dict):
                    raise ValueError("Each measurement must be an object")
                normalized.append(
                    {
                        "spot_type": str(item.get("spot_type", "")).strip().lower(),
                        "thickness_nm": float(item.get("thickness_nm")),
                        "goodness_of_fit": float(item.get("goodness_of_fit")),
                    }
                )

            exposed, blank, gof = self.__rows_to_metrics(normalized)
            self.__metrics.save_ellipsometry_data(exposure_uuid, exposed, blank, gof)
            self.__respond_ok(handle, {"exposure_uuid": str(exposure_uuid), "saved_count": len(normalized)})
        except Exception as e:
            self.__respond_err(handle, str(e))

    def ok(self):
        return self.__run and self.__client.ok() and self.__daemon.is_ok()

    def close(self):
        self.__run = False
        self.__daemon.stop()
        self.__metrics.close()
        self.__client.close()
        self.__logger_sock.close()


def main(stop_event=None):
    subsystem = DevelopmentMetricsBridgeSubsystem()
    print("Development metrics bridge subsystem started.")

    try:
        while subsystem.ok() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down development metrics bridge subsystem...")
        subsystem.close()


if __name__ == "__main__":
    main(None)
