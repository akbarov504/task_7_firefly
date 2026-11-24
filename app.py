import subprocess
import os
import signal
import sys
import time
import threading
import requests
from datetime import datetime, timedelta, timezone

from config import (
    LOCAL_PATH,
    VIDEO_SEGMENT_LEN,
    OUT_VIDEO_DEVICE,
    OUT_AUDIO_DEVICE,
    OUT_PIXEL_FORMAT,
    IN_VIDEO_DEVICE,
    IN_AUDIO_DEVICE,
    IN_INPUT_FORMAT,
    OUT_VIRTUAL_VIDEO_DEVICE,
    OUT_VIRTUAL_VIDEO_DEVICE_2,
    IN_VIRTUAL_VIDEO_DEVICE,
    IN_VIRTUAL_VIDEO_DEVICE_2,
    OUT_VIRTUAL_AUDIO_SINK,
    IN_VIRTUAL_AUDIO_SINK,
    VIRTUAL_AUDIO_RATE,
    VIRTUAL_AUDIO_CHANNELS,
)
from db import init_db, insert_video, video_exists
from utils.token_manager import get_shared_token

OUTPUT_DIR   = LOCAL_PATH
SEGMENT_TIME = VIDEO_SEGMENT_LEN

WIDTH  = 1920
HEIGHT = 1080
FPS    = 20

VIRTUAL_WIDTH  = 1280
VIRTUAL_HEIGHT = 720
VIRTUAL_FPS    = 20

RECONNECT_DELAY     = 3
DB_SCAN_INTERVAL    = 2
FILE_STABLE_SECONDS = 2

DEVICE_INFO_URL      = "http://127.0.0.1:8787/info"
TRUCK_ID_RETRY_DELAY = 3

VIDEO_ID_NAMESPACE = None
FFMPEG_ENV = {**os.environ, "TZ": "UTC"}
FFMPEG_ENV.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

os.makedirs(OUTPUT_DIR, exist_ok=True)

stop_event   = threading.Event()
processes    = {}
process_lock = threading.Lock()

def fetch_truck_id(retry_delay: int = TRUCK_ID_RETRY_DELAY) -> str:
    while True:
        try:
            token = get_shared_token()
            response = requests.get(
                DEVICE_INFO_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            truck_id = data.get("truckId")
            if truck_id is None:
                raise ValueError(f"No 'truckId' in response: {data}")
            print(f"[INFO] truckId olindi: {truck_id}")
            return str(truck_id)
        except Exception as e:
            print(f"[WARN] truckId olishda xato: {e}. {retry_delay}s dan keyin qayta urinish...")
            time.sleep(retry_delay)

def build_ffmpeg_command(
    video_device,
    audio_device,
    channels,
    sample_rate,
    prefix,
    input_format=None,
    pixel_format=None,
    virtual_video_device=None,
    virtual_video_device_2=None,
    virtual_audio_sink=None,
):
    timestamp_pattern = os.path.join(
        OUTPUT_DIR,
        f"{prefix}_%Y-%m-%d_%H-%M-%S.mp4"
    )

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-fflags", "+genpts",
        "-probesize", "5M",
        "-analyzeduration", "5M",
        "-thread_queue_size", "4096",
        "-f", "v4l2",
    ]

    if input_format:
        cmd += ["-input_format", input_format]

    if pixel_format:
        cmd += ["-pixel_format", pixel_format]

    # USB/MJPEG kamera uchun framerate
    # MIPI kamera o'z oqim tezligini ta'minlaydi
    if input_format == "mjpeg":
        cmd += ["-framerate", str(FPS)]

    cmd += [
        "-video_size", f"{WIDTH}x{HEIGHT}",
        "-use_wallclock_as_timestamps", "1",
        "-i", video_device,
    ]

    # Audio input (ALSA)
    if audio_device:
        cmd += [
            "-thread_queue_size", "4096",
            "-f", "alsa",
            "-ac", str(channels),
            "-ar", str(sample_rate),
            "-i", audio_device,
        ]

    cmd += ["-max_muxing_queue_size", "4096"]

    # Asosiy segment fayliga yozish
    cmd += ["-map", "0:v:0"]

    if audio_device:
        cmd += [
            "-map", "1:a:0",
            "-c:a", "aac",
            "-b:a", "64k",
            "-af", "aresample=async=1:first_pts=0",
        ]
    else:
        cmd += ["-an"]

    # Video encoding
    cmd += [
        "-c:v", "h264_rkmpp",
        "-b:v", "1800k",
        "-g", str(FPS * SEGMENT_TIME),
        "-keyint_min", str(FPS * SEGMENT_TIME),
        "-maxrate", "1800k",
        "-bufsize", "3600k",
        "-r", str(FPS),
        "-fps_mode", "cfr",
        "-pix_fmt", "yuv420p",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{SEGMENT_TIME})",
        "-f", "segment",
        "-segment_time", str(SEGMENT_TIME),
        "-segment_atclocktime", "1",
        "-segment_format", "mp4",
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-movflags", "+faststart",
        timestamp_pattern,
    ]

    # 1-virtual video qurilma
    if virtual_video_device:
        cmd += [
            "-map", "0:v:0",
            "-an",
            "-vf", (
                f"fps={VIRTUAL_FPS},"
                f"scale={VIRTUAL_WIDTH}:{VIRTUAL_HEIGHT}:flags=fast_bilinear,"
                f"format=yuv420p"
            ),
            "-pix_fmt", "yuv420p",
            "-f", "v4l2",
            virtual_video_device,
        ]

    # 2-virtual video qurilma
    if virtual_video_device_2:
        cmd += [
            "-map", "0:v:0",
            "-an",
            "-vf", (
                f"fps={VIRTUAL_FPS},"
                f"scale={VIRTUAL_WIDTH}:{VIRTUAL_HEIGHT}:flags=fast_bilinear,"
                f"format=yuv420p"
            ),
            "-pix_fmt", "yuv420p",
            "-f", "v4l2",
            virtual_video_device_2,
        ]

    # Virtual microphone
    if audio_device and virtual_audio_sink:
        cmd += [
            "-map", "1:a:0",
            "-vn",
            "-c:a", "pcm_s16le",
            "-ar", str(VIRTUAL_AUDIO_RATE),
            "-ac", str(VIRTUAL_AUDIO_CHANNELS),
            "-flush_packets", "1",
            "-f", "s16le",
            "pipe:1",
        ]

    return cmd

def check_video_device_exists(device_path):
    return os.path.exists(device_path)

def check_virtual_device_exists(device_path):
    return os.path.exists(device_path)

def pulse_sink_exists(sink_name):
    if not sink_name:
        return False
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True, timeout=3, env=FFMPEG_ENV,
        ).stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) > 1 and parts[1] == sink_name:
                return True
        return False
    except Exception:
        return False

def pump_audio_to_sink(src, sink_name, name):
    pacat = None
    try:
        pacat = subprocess.Popen(
            [
                "pacat", "--playback",
                "-d", sink_name,
                f"--rate={VIRTUAL_AUDIO_RATE}",
                f"--channels={VIRTUAL_AUDIO_CHANNELS}",
                "--format=s16le",
                "--latency-msec=50",
                f"--stream-name={name}_to_vmic",
            ],
            stdin=subprocess.PIPE,
            env=FFMPEG_ENV,
        )
    except Exception as e:
        print(f"[WARN] {name}: pacat ishga tushmadi: {e} (yozuv davom etadi)")

    fd = src.fileno()
    try:
        while True:
            data = os.read(fd, 4096)
            if not data:
                break
            if pacat is not None:
                try:
                    pacat.stdin.write(data)
                    pacat.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    print(f"[WARN] {name}: pacat to'xtadi, virtual audio o'chirildi")
                    pacat = None
    except Exception as e:
        print(f"[WARN] {name}: audio pump xatosi: {e}")
    finally:
        try:
            src.close()
        except Exception:
            pass
        if pacat is not None:
            try:
                pacat.stdin.close()
            except Exception:
                pass
            try:
                pacat.wait(timeout=2)
            except Exception:
                pacat.kill()

def terminate_process(proc, name):
    if not proc:
        return

    if proc.poll() is None:
        print(f"[INFO] {name}: ffmpeg to'xtatilmoqda...")
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            print(f"[WARN] {name}: ffmpeg kill qilinmoqda...")
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

def parse_segment_times_from_filename(file_name: str):
    base_name        = os.path.basename(file_name)
    name_without_ext = os.path.splitext(base_name)[0]
    parts            = name_without_ext.split("_", 1)
    if len(parts) != 2:
        return None, None, None, None

    camera_type, dt_part = parts

    try:
        actual_dt  = datetime.strptime(dt_part, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
        actual_ts  = actual_dt.timestamp()
        rounded_ts = (int(actual_ts) // SEGMENT_TIME) * SEGMENT_TIME
        slot_dt    = datetime.fromtimestamp(rounded_ts, tz=timezone.utc)

        segment_key = slot_dt.strftime("%Y-%m-%d_%H-%M-%S")
        start_dt    = slot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_dt      = (slot_dt + timedelta(seconds=SEGMENT_TIME)).strftime("%Y-%m-%dT%H:%M:%SZ")

        return camera_type, start_dt, end_dt, segment_key
    except ValueError:
        return None, None, None, None

def make_global_video_id(segment_key: str) -> str:
    return str(VIDEO_ID_NAMESPACE + "_" + segment_key)

def is_file_stable(file_path: str, stable_seconds: int = FILE_STABLE_SECONDS) -> bool:
    if not os.path.exists(file_path):
        return False

    size1 = os.path.getsize(file_path)
    time.sleep(stable_seconds)

    if not os.path.exists(file_path):
        return False

    size2 = os.path.getsize(file_path)
    return size1 == size2 and size2 > 0

def scan_and_insert_segments():
    print("[INFO] Segment DB watcher ishga tushdi")

    while not stop_event.is_set():
        try:
            files = sorted(
                f for f in os.listdir(OUTPUT_DIR)
                if f.lower().endswith(".mp4")
            )

            for file_name in files:
                file_path = os.path.join(OUTPUT_DIR, file_name)

                if video_exists(file_path):
                    continue

                if not is_file_stable(file_path):
                    continue

                camera_type, start_time, end_time, segment_key = \
                    parse_segment_times_from_filename(file_name)

                if not camera_type:
                    continue

                ideal_file_name = f"{camera_type}_{segment_key}.mp4"
                ideal_file_path = os.path.join(OUTPUT_DIR, ideal_file_name)

                if file_name != ideal_file_name:
                    try:
                        os.rename(file_path, ideal_file_path)
                        file_path = ideal_file_path
                        print(f"[INFO] Fayl vaqti sinxronlandi: {file_name} -> {ideal_file_name}")
                    except Exception as e:
                        print(f"[ERROR] Fayl nomini o'zgartirishda xato: {e}")
                        continue

                if video_exists(file_path):
                    continue

                global_video_id = make_global_video_id(segment_key)

                insert_video(
                    file_path=file_path,
                    camera_type=camera_type,
                    start_time=start_time,
                    end_time=end_time,
                    globalVideoId=global_video_id
                )

                print(
                    f"[DB] Video saqlandi: "
                    f"camera_type={camera_type}, "
                    f"globalVideoId={global_video_id}, "
                    f"start={start_time}, "
                    f"end={end_time}"
                )

        except Exception as e:
            print(f"[DB WATCHER ERROR] {e}")

        time.sleep(DB_SCAN_INTERVAL)

def wait_for_sync(segment_time):
    now = time.time()
    next_boundary = ((int(now) // segment_time) + 1) * segment_time
    sleep_time = next_boundary - now + 0.1
    time.sleep(sleep_time)

def camera_worker(name, video_device, audio_device, input_format, pixel_format,
                  virtual_video_device, virtual_video_device_2,
                  virtual_audio_sink):
    global processes

    while not stop_event.is_set():
        video_ok     = check_video_device_exists(video_device)
        virtual_ok   = check_virtual_device_exists(virtual_video_device)
        virtual_ok_2 = check_virtual_device_exists(virtual_video_device_2)

        if not video_ok:
            print(f"[WARN] {name}: video device yo'q -> {video_device}")
            time.sleep(RECONNECT_DELAY)
            continue

        if not virtual_ok:
            print(f"[WARN] {name}: virtual device yo'q -> {virtual_video_device}")
            time.sleep(RECONNECT_DELAY)
            continue

        if not virtual_ok_2:
            print(f"[WARN] {name}: 2-virtual device yo'q -> {virtual_video_device_2}")
            time.sleep(RECONNECT_DELAY)
            continue

        audio_sink = None
        if audio_device and virtual_audio_sink and pulse_sink_exists(virtual_audio_sink):
            audio_sink = virtual_audio_sink
        elif audio_device and virtual_audio_sink:
            print(f"[WARN] {name}: virtual audio sink yo'q -> {virtual_audio_sink} (yozuv davom etadi)")

        cmd = build_ffmpeg_command(
            video_device=video_device,
            audio_device=audio_device,
            channels="2",
            sample_rate="48000",
            prefix=name,
            input_format=input_format,
            pixel_format=pixel_format,
            virtual_video_device=virtual_video_device,
            virtual_video_device_2=virtual_video_device_2,
            virtual_audio_sink=audio_sink,
        )

        fmt_desc = input_format or pixel_format or "auto"
        audio_desc = audio_device if audio_device else "None (audiosiz)"

        print(f"[INFO] {name}: ffmpeg sozlamalari tayyorlandi")
        print(f"[INFO] {name}: VIDEO={video_device} ({fmt_desc})")
        print(f"[INFO] {name}: AUDIO={audio_desc}")
        print(f"[INFO] {name}: VIRTUAL={virtual_video_device}")
        print(f"[INFO] {name}: VIRTUAL_2={virtual_video_device_2}")

        print(f"[INFO] {name}: Sinxron boshlash uchun vaqt kutilmoqda...")
        wait_for_sync(SEGMENT_TIME)

        print(f"[INFO] {name}: ffmpeg ishga tushirildi! (TZ=UTC)")
        if audio_sink:
            proc = subprocess.Popen(cmd, env=FFMPEG_ENV, stdout=subprocess.PIPE)
            threading.Thread(
                target=pump_audio_to_sink,
                args=(proc.stdout, audio_sink, name),
                daemon=True,
            ).start()
        else:
            proc = subprocess.Popen(cmd, env=FFMPEG_ENV)

        with process_lock:
            processes[name] = proc

        while not stop_event.is_set():
            ret = proc.poll()
            if ret is not None:
                print(f"[WARN] {name}: ffmpeg to'xtab qoldi (code={ret}). Qayta ulanish...")
                break
            time.sleep(1)

        terminate_process(proc, name)

        with process_lock:
            processes[name] = None

        if not stop_event.is_set():
            time.sleep(RECONNECT_DELAY)

def stop_all(signum=None, frame=None):
    print("\n[INFO] Dastur to'xtatilmoqda...")
    stop_event.set()

    with process_lock:
        for name, proc in processes.items():
            terminate_process(proc, name)

    print("[INFO] Hamma jarayonlar to'xtatildi.")
    sys.exit(0)

def main():
    global VIDEO_ID_NAMESPACE

    init_db()

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    if not check_virtual_device_exists(OUT_VIRTUAL_VIDEO_DEVICE):
        print(f"[ERROR] Virtual device topilmadi: {OUT_VIRTUAL_VIDEO_DEVICE}")
        sys.exit(1)

    if not check_virtual_device_exists(IN_VIRTUAL_VIDEO_DEVICE):
        print(f"[ERROR] Virtual device topilmadi: {IN_VIRTUAL_VIDEO_DEVICE}")
        sys.exit(1)

    if not check_virtual_device_exists(OUT_VIRTUAL_VIDEO_DEVICE_2):
        print(f"[ERROR] Virtual device topilmadi: {OUT_VIRTUAL_VIDEO_DEVICE_2}")
        sys.exit(1)

    if not check_virtual_device_exists(IN_VIRTUAL_VIDEO_DEVICE_2):
        print(f"[ERROR] Virtual device topilmadi: {IN_VIRTUAL_VIDEO_DEVICE_2}")
        sys.exit(1)

    print("[INFO] truckId olinmoqda...")
    VIDEO_ID_NAMESPACE = fetch_truck_id()
    print(f"[INFO] VIDEO_ID_NAMESPACE = {VIDEO_ID_NAMESPACE}")

    print("[INFO] Auto-reconnect recording system boshlandi")
    print(f"[INFO] Papka: {OUTPUT_DIR}")
    print(f"[INFO] Segment: {SEGMENT_TIME} sekund")
    print(f"[INFO] Virtual stream: {VIRTUAL_WIDTH}x{VIRTUAL_HEIGHT} @ {VIRTUAL_FPS} fps")
    print("[INFO] ffmpeg TZ=UTC bilan ishga tushiriladi — fayl nomlari UTC da bo'ladi")
    print("[INFO] DB ga saqlanadigan vaqtlar: ISO 8601 UTC (Z qo'shimchasi bilan)")
    print("[INFO] To'xtatish uchun CTRL+C bosing\n")

    db_thread = threading.Thread(target=scan_and_insert_segments, daemon=True)

    out_thread = threading.Thread(
        target=camera_worker,
        args=(
            "OUT",
            OUT_VIDEO_DEVICE,
            OUT_AUDIO_DEVICE,           # None (audiosiz)
            None,                       # input_format
            OUT_PIXEL_FORMAT,           # pixel_format='nv12'
            OUT_VIRTUAL_VIDEO_DEVICE,
            OUT_VIRTUAL_VIDEO_DEVICE_2,
            OUT_VIRTUAL_AUDIO_SINK,     # None
        ),
        daemon=True
    )

    in_thread = threading.Thread(
        target=camera_worker,
        args=(
            "IN",
            IN_VIDEO_DEVICE,
            IN_AUDIO_DEVICE,            # hw:1,0 (Arducam mic)
            IN_INPUT_FORMAT,            # input_format='mjpeg'
            None,                       # pixel_format
            IN_VIRTUAL_VIDEO_DEVICE,
            IN_VIRTUAL_VIDEO_DEVICE_2,
            IN_VIRTUAL_AUDIO_SINK,      # IN_VMIC_SINK
        ),
        daemon=True
    )

    db_thread.start()
    out_thread.start()
    in_thread.start()

    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            stop_all()

if __name__ == "__main__":
    main()
