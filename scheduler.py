import os
import sys
import time
import subprocess
import signal
import atexit
import json
import socket
import fcntl
from datetime import datetime, timedelta
from utils_holiday import is_market_open, get_holiday_name

# 설정: KIS API 서버 점검 시간 등
KIS_MAINTENANCE_HOURS = [(23, 0), (0, 30)]  # 23:00 ~ 00:30 (예시)


def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def is_internet_connected():
    """인터넷 연결 확인 (구글 DNS 핑)"""
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False


def is_maintenance_time():
    """서버 점검 시간인지 확인.

    Recommended behavior:
    - Keep the rule simple and conservative.
    - Support a configurable window list (KIS_MAINTENANCE_HOURS) while also
      guarding the common 04:00~05:59 maintenance region.
    """
    now = datetime.now()

    # Common broker maintenance window.
    if now.hour in (4, 5):
        return True

    # Optional configured windows (hour, minute) tuples.
    try:
        (h1, m1), (h2, m2) = KIS_MAINTENANCE_HOURS
        start = now.replace(hour=int(h1), minute=int(m1), second=0, microsecond=0)
        end = now.replace(hour=int(h2), minute=int(m2), second=0, microsecond=0)
        # handle crossing midnight
        if end <= start:
            if now >= start:
                return True
            end = end + timedelta(days=1)
            if now < start:
                now_cmp = now + timedelta(days=1)
            else:
                now_cmp = now
            return start <= now_cmp <= end
        return start <= now <= end
    except Exception:
        return False


def get_schedule(config):
    time_rules = config.get("time_rules", {})
    observe_start_str = time_rules.get("observe_start", "08:50:00")
    market_close_str = "15:35:00"  # 장 마감 후 정리 시간 포함

    now = datetime.now()

    obs_dt = datetime.strptime(observe_start_str, "%H:%M:%S").replace(
        year=now.year, month=now.month, day=now.day
    )
    start_dt = obs_dt - timedelta(minutes=10)  # 8:40부터 준비

    close_dt = datetime.strptime(market_close_str, "%H:%M:%S").replace(
        year=now.year, month=now.month, day=now.day
    )

    return start_dt, close_dt


def _find_running_bot_pids() -> list[int]:
    """이미 실행 중인 main.py가 있으면 PID 목록을 반환합니다.

    ✅ 중요: 실제 프로세스 cmdline은 보통
      /.../venv/bin/python main.py
    처럼 'main.py' 앞에 절대경로가 없을 수 있어, 패턴을 넓게 잡습니다.

    스케줄러(process 변수)가 꼬이거나, 스케줄러가 재시작된 경우에도
    중복 실행을 막기 위한 안전장치입니다.
    """
    patterns = [
        r"kis_orb_vwap_bot/venv/bin/python.*main\.py",
        r"venv/bin/python.*main\.py",
    ]
    pids: set[int] = set()
    for pat in patterns:
        try:
            out = subprocess.check_output(["pgrep", "-f", pat], text=True).strip()
            if out:
                for x in out.splitlines():
                    x = x.strip()
                    if x.isdigit():
                        pids.add(int(x))
        except subprocess.CalledProcessError:
            continue
        except Exception:
            continue
    return sorted(pids)


def _acquire_scheduler_lock():
    """스케줄러 중복 실행 방지 (Lock)"""
    lock_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".kis_scheduler.lock"
    )
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[Scheduler] Already running. Exiting.")
        sys.exit(0)
    return f


def _stop_child_process(process, log_file) -> tuple[None, None]:
    if process is None:
        if log_file:
            try:
                log_file.close()
            except Exception:
                pass
        return None, None
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except Exception:
            pass
    if log_file:
        try:
            log_file.close()
        except Exception:
            pass
    return None, None


def main():
    # 락 획득 (실패 시 자동 종료)
    _lock = _acquire_scheduler_lock()

    process = None
    log_file = None
    last_launch_ts = 0.0
    relaunch_backoff_sec = 75  # 토큰 1분당 1회 제한(EGW00133) + 여유
    running = True

    def _request_shutdown(signum, _frame):
        nonlocal running
        print(f"[Scheduler] signal received: {signum}. shutting down.")
        running = False

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    def _cleanup():
        nonlocal process, log_file
        process, log_file = _stop_child_process(process, log_file)
        try:
            fcntl.flock(_lock.fileno(), fcntl.LOCK_UN)
            _lock.close()
        except Exception:
            pass

    atexit.register(_cleanup)

    print(f"[Scheduler] Immortal System Started. PID={os.getpid()}")

    while running:
        try:
            # 1. 인터넷 연결 체크
            if not is_internet_connected():
                print("[Scheduler] Network Offline. Waiting...")
                time.sleep(30)
                continue

            # 2. 서버 점검 시간 체크
            if is_maintenance_time():
                print("[Scheduler] Server Maintenance Time. Sleeping...")
                time.sleep(600)  # 10분 대기
                continue

            now = datetime.now()

            # 3. 휴장일 체크 (주말/공휴일)
            if not is_market_open(now.date()):
                if now.hour == 9 and now.minute == 0:  # 하루 한 번만 로그
                    print(
                        f"[Scheduler] Market Closed Today: {get_holiday_name(now.date())}"
                    )

                # 장 마감 시간 이후면 내일 날짜로 넘어가기 위해 짧게 대기, 아니면 길게 대기
                time.sleep(3600)
                continue

            # 4. 실행 스케줄 확인
            config = load_config()
            start_dt, stop_dt = get_schedule(config)

            is_running_time = start_dt <= now < stop_dt

            if is_running_time:
                # 이미 다른 프로세스로 main.py가 떠 있으면 중복 실행 금지
                running_pids = _find_running_bot_pids()
                if running_pids:
                    # scheduler가 재기동됐거나 process handle을 잃은 경우를 대비
                    if process is None or process.poll() is not None:
                        process = None
                    # 아무 것도 하지 않고 유지
                else:
                    # 봇이 죽어있으면 살린다(단, 재기동 폭주 방지)
                    need_launch = (process is None) or (process.poll() is not None)
                    if need_launch:
                        since = time.time() - last_launch_ts
                        if since < relaunch_backoff_sec:
                            wait = int(relaunch_backoff_sec - since)
                            print(
                                f"[Scheduler] Bot recently launched. Backing off {wait}s..."
                            )
                        else:
                            print(f"[Scheduler] Launching Bot... ({now})")

                            # 로그 파일 분리 (날짜별)
                            log_path = f"logs/bot_{now.strftime('%Y%m%d')}.log"
                            os.makedirs("logs", exist_ok=True)
                            if log_file:
                                try:
                                    log_file.close()
                                except Exception:
                                    pass
                            log_file = open(log_path, "a", encoding="utf-8")

                            # 봇 실행
                            process = subprocess.Popen(
                                [sys.executable, "main.py"],
                                stdout=log_file,
                                stderr=subprocess.STDOUT,
                                cwd=os.getcwd(),  # 현재 디렉토리 유지
                            )
                            last_launch_ts = time.time()
                            print(f"[Scheduler] Bot PID: {process.pid}")

            else:
                # 운영 시간이 아니면 봇 종료
                if process is not None:
                    print("[Scheduler] Stopping Bot (Market Closed).")
                    process, log_file = _stop_child_process(process, log_file)

                    # 일일 리포트 생성
                    try:
                        print("[Scheduler] Generating Daily Report...")
                        # pandas 등 무거운 의존성은 장중에 불필요하므로 여기서 늦게 import
                        from reporter import generate_daily_report

                        base_dir = os.path.dirname(os.path.abspath(__file__))
                        report = generate_daily_report(base_dir)
                        print(f"[Scheduler] Report Saved: {report}")
                    except Exception as e:
                        print(f"[Scheduler] Report Error: {e}")
                        base_dir = os.path.dirname(os.path.abspath(__file__))

                    # 주간 리포트 생성 (매주 월요일 09:00)
                    if now.weekday() == 0 and now.hour == 9 and now.minute == 0:
                        try:
                            print("[Scheduler] Generating Weekly Report...")
                            from reporter import KisReporter

                            reporter = KisReporter(base_dir)
                            report, _ = reporter.generate_weekly_report()
                            print(f"[Scheduler] Weekly Report Saved: {report}")
                        except Exception as e:
                            print(f"[Scheduler] Weekly Report Error: {e}")

                    # 월간 리포트 생성 (매월 1일 09:00)
                    if now.day == 1 and now.hour == 9 and now.minute == 0:
                        try:
                            print("[Scheduler] Generating Monthly Report...")
                            from reporter import KisReporter

                            reporter = KisReporter(base_dir)
                            report, _ = reporter.generate_monthly_report()
                            print(f"[Scheduler] Monthly Report Saved: {report}")
                        except Exception as e:
                            print(f"[Scheduler] Monthly Report Error: {e}")

                # 다음 날 아침까지 긴 대기 (불필요한 CPU 소모 방지)
                if now > stop_dt:
                    next_start = start_dt + timedelta(days=1)
                    sleep_sec = (next_start - datetime.now()).total_seconds()
                    if sleep_sec > 0:
                        print(
                            f"[Scheduler] Sleep until tomorrow morning ({sleep_sec / 3600:.1f} hours)"
                        )
                        # 너무 길게 자면 중간에 깼을 때 대응이 늦으므로 1시간 단위로 깸
                        time.sleep(min(sleep_sec, 3600))
                        continue

            time.sleep(10)  # 10초마다 상태 체크

        except Exception as e:
            print(f"[Scheduler] CRITICAL ERROR: {e}")
            time.sleep(60)  # 에러 나도 죽지 않고 1분 뒤 재시도

    print("[Scheduler] Stop complete.")


if __name__ == "__main__":
    main()
