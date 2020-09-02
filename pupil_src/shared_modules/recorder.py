"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2020 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""

import errno

import glob
import logging
import os
import uuid
from shutil import copy2
from time import gmtime, localtime, strftime, time

import psutil
from ndsi import H264Writer
from pyglui import ui

import csv_utils
from av_writer import MPEG_Writer, JPEG_Writer, NonMonotonicTimestampError
from file_methods import PLData_Writer, load_object
from methods import get_system_info, timer
from video_capture.ndsi_backend import NDSI_Source

from pupil_recording.info import RecordingInfoFile

from gaze_mapping.notifications import (
    CalibrationSetupNotification,
    CalibrationResultNotification,
)

# from scipy.interpolate import UnivariateSpline
from plugin import System_Plugin_Base

logger = logging.getLogger(__name__)


def get_auto_name():
    return strftime("%Y_%m_%d", localtime())


def available_gb(path):
    num_avail_gb = psutil.disk_usage(path).free / 1e9
    # logger.debug('{} has {:.2f} GB available'.format(path, num_avail_gb))
    return num_avail_gb


class Recorder(System_Plugin_Base):
    """Capture Recorder"""

    icon_chr = chr(0xE04B)
    icon_font = "pupil_icons"
    warning_low_disk_space_th = 5.0  # threshold in GB
    stop_rec_low_disk_space_th = 1.0  # threshold in GB

    def __init__(
        self,
        g_pool,
        session_name=get_auto_name(),
        rec_root_dir=None,
        user_info={"name": "", "additional_field": "change_me"},
        info_menu_conf={},
        show_info_menu=False,
        record_eye=True,
        raw_jpeg=True,
    ):
        super().__init__(g_pool)
        # update name if it was autogenerated.
        if session_name.startswith("20") and len(session_name) == 10:
            session_name = get_auto_name()

        base_dir = self.g_pool.user_dir.rsplit(os.path.sep, 1)[0]
        default_rec_root_dir = os.path.join(base_dir, "recordings")

        if (
            rec_root_dir
            and rec_root_dir != default_rec_root_dir
            and self.verify_path(rec_root_dir)
        ):
            self.rec_root_dir = rec_root_dir
        else:
            try:
                os.makedirs(default_rec_root_dir)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    logger.error("Could not create Rec dir")
                    raise e
            else:
                logger.info(
                    'Created standard Rec dir at "{}"'.format(default_rec_root_dir)
                )
            self.rec_root_dir = default_rec_root_dir

        self.raw_jpeg = raw_jpeg
        self.order = 0.9
        self.record_eye = record_eye
        self.session_name = session_name
        self.running = False
        self.menu = None
        self.button = None

        self.user_info = user_info
        self.show_info_menu = show_info_menu
        self.info_menu = None
        self.info_menu_conf = info_menu_conf

        self.low_disk_space_thumb = None
        check_timer = timer(1.0)
        self.check_space = lambda: next(check_timer)

    def get_init_dict(self):
        d = {}
        d["record_eye"] = self.record_eye
        d["session_name"] = self.session_name
        d["user_info"] = self.user_info
        d["info_menu_conf"] = self.info_menu_conf
        d["show_info_menu"] = self.show_info_menu
        d["rec_root_dir"] = self.rec_root_dir
        d["raw_jpeg"] = self.raw_jpeg
        return d

    def init_ui(self):
        self.add_menu()
        self.menu.label = "Recorder"
        self.menu_icon.order = 0.29

        self.menu.append(
            ui.Info_Text(
                'Pupil recordings are saved like this: "path_to_recordings/recording_session_name/nnn" where "nnn" is an increasing number to avoid overwrites. You can use "/" in your session name to create subdirectories.'
            )
        )
        self.menu.append(
            ui.Info_Text(
                'Recordings are saved to "~/pupil_recordings". You can change the path here but note that invalid input will be ignored.'
            )
        )
        self.menu.append(
            ui.Text_Input(
                "rec_root_dir",
                self,
                setter=self.set_rec_root_dir,
                label="Path to recordings",
            )
        )
        self.menu.append(
            ui.Text_Input(
                "session_name",
                self,
                setter=self.set_session_name,
                label="Recording session name",
            )
        )
        self.menu.append(
            ui.Switch(
                "show_info_menu",
                self,
                on_val=True,
                off_val=False,
                label="Request additional user info",
            )
        )
        self.menu.append(
            ui.Selector(
                "raw_jpeg",
                self,
                selection=[True, False],
                labels=["bigger file, less CPU", "smaller file, more CPU"],
                label="Compression",
            )
        )
        self.menu.append(
            ui.Info_Text(
                "Recording the raw eye video is optional. We use it for debugging."
            )
        )
        self.menu.append(
            ui.Switch(
                "record_eye", self, on_val=True, off_val=False, label="Record eye"
            )
        )
        self.button = ui.Thumb(
            "running", self, setter=self.toggle, label="R", hotkey="r"
        )
        self.button.on_color[:] = (1, 0.0, 0.0, 0.8)
        self.g_pool.quickbar.insert(2, self.button)

        self.low_disk_space_thumb = ui.Thumb(
            "low_disk_warn", label="!", getter=lambda: True, setter=lambda x: None
        )
        self.low_disk_space_thumb.on_color[:] = (1, 0.0, 0.0, 0.8)
        self.low_disk_space_thumb.status_text = "Low disk space"

    def deinit_ui(self):
        if self.low_disk_space_thumb in self.g_pool.quickbar:
            self.g_pool.quickbar.remove(self.low_disk_space_thumb)
        self.g_pool.quickbar.remove(self.button)
        self.button = None
        self.remove_menu()

    def toggle(self, _=None):
        if self.running:
            self.notify_all({"subject": "recording.should_stop"})
            self.notify_all(
                {"subject": "recording.should_stop", "remote_notify": "all"}
            )
        else:
            self.notify_all(
                {"subject": "recording.should_start", "session_name": self.session_name}
            )
            self.notify_all(
                {
                    "subject": "recording.should_start",
                    "session_name": self.session_name,
                    "remote_notify": "all",
                }
            )

    def on_notify(self, notification):
        """Handles recorder notifications

        Reacts to notifications:
            ``recording.should_start``: Starts a new recording session.
                fields:
                - 'session_name' change session name
                    start with `/` to ingore the rec base dir and start from root instead.
                - `record_eye` boolean that indicates recording of the eyes, defaults to current setting
            ``recording.should_stop``: Stops current recording session

        Emits notifications:
            ``recording.started``: New recording session started
            ``recording.stopped``: Current recording session stopped

        Args:
            notification (dictionary): Notification dictionary
        """
        # notification wants to be recorded
        if notification.get("record", False) and self.running:
            if "timestamp" not in notification:
                logger.error("Notification without timestamp will not be saved.")
                notification["timestamp"] = self.g_pool.get_timestamp()
            # else:
            notification["topic"] = "notify." + notification["subject"]
            try:
                writer = self.pldata_writers["notify"]
            except KeyError:
                writer = PLData_Writer(self.rec_path, "notify")
                self.pldata_writers["notify"] = writer
            writer.append(notification)

        elif notification["subject"] == "recording.should_start":
            if self.running:
                logger.info("Recording already running!")
            else:
                self.record_eye = notification.get("record_eye", self.record_eye)
                if notification.get("session_name", ""):
                    self.set_session_name(notification["session_name"])
                self.start()

        elif notification["subject"] == "recording.should_stop":
            if self.running:
                self.stop()
            else:
                logger.info("Recording already stopped!")

    def get_rec_time_str(self):
        rec_time = gmtime(time() - self.start_time)
        return strftime("%H:%M:%S", rec_time)

    def start(self):
        self.start_time = time()
        start_time_synced = self.g_pool.get_timestamp()

        if isinstance(self.g_pool.capture, NDSI_Source):
            # If the user did not enable TimeSync, the timestamps will be way off and
            # the recording code will crash. We check the difference between the last
            # frame's time and the start_time_synced and if this does not match, we stop
            # the recording and show a warning instead.
            TIMESTAMP_ERROR_THRESHOLD = 5.0
            frame = self.g_pool.capture._recent_frame
            if frame is None:
                logger.error(
                    "Your connection does not seem to be stable enough for "
                    "recording Pupil Mobile via WiFi. We recommend recording "
                    "on the phone."
                )
                return
            if abs(frame.timestamp - start_time_synced) > TIMESTAMP_ERROR_THRESHOLD:
                logger.error(
                    "Pupil Mobile stream is not in sync. Aborting recording."
                    " Enable the Time Sync plugin and try again."
                )
                return

        session = os.path.join(self.rec_root_dir, self.session_name)
        try:
            os.makedirs(session, exist_ok=True)
            logger.debug("Created new recordings session dir {}".format(session))
        except OSError:
            logger.error(
                "Could not start recording. Session dir {} not writable.".format(
                    session
                )
            )
            return

        self.pldata_writers = {}
        self.frame_count = 0
        self.running = True
        self.menu.read_only = True
        recording_uuid = uuid.uuid4()

        # set up self incrementing folder within session folder
        counter = 0
        while True:
            self.rec_path = os.path.join(session, "{:03d}/".format(counter))
            try:
                os.mkdir(self.rec_path)
                logger.debug("Created new recording dir {}".format(self.rec_path))
                break
            except FileExistsError:
                logger.debug(
                    "We dont want to overwrite data, incrementing counter & trying to make new data folder"
                )
                counter += 1

        self.meta_info = RecordingInfoFile.create_empty_file(self.rec_path)
        self.meta_info.recording_software_name = (
            RecordingInfoFile.RECORDING_SOFTWARE_NAME_PUPIL_CAPTURE
        )
        self.meta_info.recording_software_version = self.g_pool.version.vstring
        self.meta_info.recording_name = self.session_name
        self.meta_info.start_time_synced_s = start_time_synced
        self.meta_info.start_time_system_s = self.start_time
        self.meta_info.recording_uuid = recording_uuid
        self.meta_info.system_info = get_system_info()

        self.video_path = os.path.join(self.rec_path, "world.mp4")
        if self.raw_jpeg and self.g_pool.capture.jpeg_support:
            self.writer = JPEG_Writer(self.video_path, start_time_synced)
        elif hasattr(self.g_pool.capture._recent_frame, "h264_buffer"):
            self.writer = H264Writer(
                self.video_path,
                self.g_pool.capture.frame_size[0],
                self.g_pool.capture.frame_size[1],
                int(self.g_pool.capture.frame_rate),
            )
        else:
            self.writer = MPEG_Writer(self.video_path, start_time_synced)

        calibration_data_notification_classes = [
            CalibrationSetupNotification,
            CalibrationResultNotification,
        ]
        writer = PLData_Writer(self.rec_path, "notify")

        for note_class in calibration_data_notification_classes:
            try:
                file_path = os.path.join(self.g_pool.user_dir, note_class.file_name())
                note = note_class.from_dict(load_object(file_path))
                note_dict = note.as_dict()

                note_dict["topic"] = "notify." + note_dict["subject"]
                writer.append(note_dict)
            except FileNotFoundError:
                continue

        self.pldata_writers["notify"] = writer

        if self.show_info_menu:
            self.open_info_menu()
        logger.info("Started Recording.")
        self.notify_all(
            {
                "subject": "recording.started",
                "rec_path": self.rec_path,
                "session_name": self.session_name,
                "record_eye": self.record_eye,
                "compression": self.raw_jpeg,
                "start_time_synced": float(start_time_synced),
            }
        )

    def open_info_menu(self):
        self.info_menu = ui.Growing_Menu(
            "additional Recording Info", size=(300, 300), pos=(300, 300)
        )
        self.info_menu.configuration = self.info_menu_conf

        def populate_info_menu():
            self.info_menu.elements[:-2] = []
            for name in self.user_info.keys():
                self.info_menu.insert(0, ui.Text_Input(name, self.user_info))

        def set_user_info(new_string):
            self.user_info = new_string
            populate_info_menu()

        populate_info_menu()
        self.info_menu.append(
            ui.Info_Text(
                'Use the *user info* field to add/remove additional fields and their values. The format must be a valid Python dictionary. For example -- {"key":"value"}. You can add as many fields as you require. Your custom fields will be saved for your next session.'
            )
        )
        self.info_menu.append(
            ui.Text_Input("user_info", self, setter=set_user_info, label="User info")
        )
        self.g_pool.gui.append(self.info_menu)

    def close_info_menu(self):
        if self.info_menu:
            self.info_menu_conf = self.info_menu.configuration
            self.g_pool.gui.remove(self.info_menu)
            self.info_menu = None

    def recent_events(self, events):

        if self.check_space():
            disk_space = available_gb(self.rec_root_dir)
            if (
                disk_space < self.warning_low_disk_space_th
                and self.low_disk_space_thumb not in self.g_pool.quickbar
            ):
                self.g_pool.quickbar.append(self.low_disk_space_thumb)
            elif (
                disk_space >= self.warning_low_disk_space_th
                and self.low_disk_space_thumb in self.g_pool.quickbar
            ):
                self.g_pool.quickbar.remove(self.low_disk_space_thumb)

            if self.running and disk_space <= self.stop_rec_low_disk_space_th:
                self.stop()
                logger.error("Recording was stopped due to low disk space!")

        if self.running:
            for key, data in events.items():
                if key not in ("dt", "depth_frame") and not key.startswith("frame"):
                    try:
                        writer = self.pldata_writers[key]
                    except KeyError:
                        writer = PLData_Writer(self.rec_path, key)
                        self.pldata_writers[key] = writer
                    writer.extend(data)
            if "frame" in events:
                frame = events["frame"]
                try:
                    self.writer.write_video_frame(frame)
                    self.frame_count += 1
                except NonMonotonicTimestampError as e:
                    logger.error(
                        "Recorder received non-monotonic timestamp!"
                        " Stopping the recording!"
                    )
                    logger.debug(str(e))
                    self.notify_all({"subject": "recording.should_stop"})
                    self.notify_all(
                        {"subject": "recording.should_stop", "remote_notify": "all"}
                    )
            # # cv2.putText(frame.img, "Frame %s"%self.frame_count,(200,200), cv2.FONT_HERSHEY_SIMPLEX,1,(255,100,100))

            self.button.status_text = self.get_rec_time_str()

    def stop(self):
        duration_s = self.g_pool.get_timestamp() - self.meta_info.start_time_synced_s

        # explicit release of VideoWriter
        try:
            self.writer.release()
        except RuntimeError:
            logger.error("No world video recorded")
        else:
            logger.debug("Closed media container")
            self.g_pool.capture.intrinsics.save(self.rec_path, custom_name="world")
        finally:
            self.writer = None

        for writer in self.pldata_writers.values():
            writer.close()

        del self.pldata_writers

        surface_definition_file_paths = glob.glob(
            os.path.join(self.g_pool.user_dir, "surface_definitions*")
        )

        if len(surface_definition_file_paths) > 0:
            for source_path in surface_definition_file_paths:
                _, filename = os.path.split(source_path)
                target_path = os.path.join(self.rec_path, filename)
                copy2(source_path, target_path)
        else:
            logger.info(
                "No surface_definitions data found. You may want this if you do marker tracking."
            )

        self.meta_info.duration_s = duration_s
        self.meta_info.save_file()

        try:
            with open(
                os.path.join(self.rec_path, "user_info.csv"), "w", newline=""
            ) as csvfile:
                csv_utils.write_key_value_file(csvfile, self.user_info)
        except OSError:
            logger.exception("Could not save userdata. Please report this bug!")

        self.close_info_menu()

        self.running = False
        if self.menu:
            self.menu.read_only = False
            self.button.status_text = ""

        logger.info("Saved Recording.")
        self.notify_all({"subject": "recording.stopped", "rec_path": self.rec_path})

    def cleanup(self):
        """gets called when the plugin get terminated.
        either volunatily or forced.
        """
        if self.running:
            self.stop()

    def verify_path(self, val):
        try:
            n_path = os.path.expanduser(val)
            logger.debug("Expanded user path.")
        except Exception:
            n_path = val
        if not n_path:
            logger.warning("Please specify a path.")
            return False
        elif not os.path.isdir(n_path):
            logger.warning("This is not a valid path.")
            return False
        # elif not os.access(n_path, os.W_OK):
        elif not writable_dir(n_path):
            logger.warning("Do not have write access to '{}'.".format(n_path))
            return False
        else:
            return n_path

    def set_rec_root_dir(self, val):
        n_path = self.verify_path(val)
        if n_path:
            self.rec_root_dir = n_path

    def set_session_name(self, val):
        if not val:
            self.session_name = get_auto_name()
        else:
            if os.path.sep in val:
                logger.warning(
                    "You session name will create one or more subdirectories"
                )
            self.session_name = val


def writable_dir(n_path):
    try:
        open(os.path.join(n_path, "dummpy_tmp"), "w")
    except IOError:
        return False
    else:
        os.remove(os.path.join(n_path, "dummpy_tmp"))
        return True
