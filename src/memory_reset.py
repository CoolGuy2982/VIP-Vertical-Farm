"""Reset the AI's memory for a new grow, archiving instead of deleting.

Moves the AI's context files into data/archive/<timestamp>/ and clears the
scheduled-action queue, so the next check-in starts fresh instead of
continuing the previous grow's story. Images, grower.log and the pump dose
log (the water budget) are left alone.

From the website: the `reset_ai_memory` command (src/remote_control.py).
On the Pi, with the service stopped:
    python -m src.memory_reset --reason "new lettuce tray"
"""

import argparse
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_FILES = [
    "data/logs/decisions.jsonl",       # recent decisions fed back into every prompt
    "data/logs/growth_summary.json",   # the AI's compressed "memory"
    "data/logs/milestones.jsonl",
    "data/logs/growth_log.jsonl",
    "data/logs/sensor_log.jsonl",
    "data/logs/action_queue.json",     # scheduled check-ins / observations / pump doses
]


def _count_lines(path: Path) -> int:
    try:
        with open(path) as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def reset_ai_memory(base_dir: str | Path, reason: str = "", grower=None,
                    archive_images: bool = True) -> dict:
    """Archive the AI's memory files (and by default the photos, which the AI
    also uses as history). With a running grower, also clear its in-memory
    scheduler queue and Gemini conversation."""
    base = Path(base_dir)
    if grower is not None and getattr(grower, "_checkin_lock", False):
        raise RuntimeError("A check-in is running; try again in a minute")

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    archive = base / "data" / "archive" / stamp
    archive.mkdir(parents=True, exist_ok=False)

    moved = {}
    for rel in MEMORY_FILES:
        src = base / rel
        if not src.exists():
            continue
        moved[rel] = {"bytes": src.stat().st_size, "lines": _count_lines(src)}
        shutil.move(str(src), str(archive / src.name))

    # Photos: the AI is shown the last few plant photos as "history", so the
    # old tray's photos must go too. (data/images/blank/ stays.)
    images_moved = 0
    if archive_images:
        image_dir = Path(getattr(getattr(grower, "camera", None), "image_dir", "") or base / "data" / "images")
        if image_dir.is_dir():
            dest = archive / "images"
            for img in image_dir.glob("*.jpg"):
                dest.mkdir(exist_ok=True)
                shutil.move(str(img), str(dest / img.name))
                images_moved += 1

    manifest = {
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "files": moved,
        "images": images_moved,
    }
    if grower is not None:
        plant = grower.config.get("plant", {})
        manifest["plant"] = {k: plant.get(k) for k in ("species", "variety", "planted_date")}
    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2))

    dropped = 0
    if grower is not None:
        dropped = grower.scheduler.clear()
        try:
            grower.gemini.reset_chain()
        except Exception:
            pass
        if hasattr(grower, "_alert_log"):
            grower._alert_log.clear()
        grower.last_checkin = None
        if not getattr(grower, "paused", False):
            grower.scheduler.schedule_checkin(
                grower.scheduler.min_checkin_minutes,
                "First check-in after AI memory reset (new grow)")

    rel_archive = archive.relative_to(base).as_posix()
    logger.warning("AI memory reset: %d files and %d photos archived to %s, "
                   "%d scheduled actions dropped (%s)",
                   len(moved), images_moved, rel_archive, dropped, reason or "no reason given")
    return {
        "message": f"AI memory and {images_moved} photos archived to {rel_archive}",
        "archive": rel_archive,
        "files": moved,
        "images": images_moved,
        "dropped_actions": dropped,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-dir", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--reason", default="manual reset from the command line")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    if not args.yes:
        print("Stop the grower first:  sudo systemctl stop ai-grower")
        if input("Archive the AI memory files now? (yes/no): ").strip() != "yes":
            print("Aborted.")
            return
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(reset_ai_memory(args.base_dir, args.reason), indent=2))


if __name__ == "__main__":
    main()
