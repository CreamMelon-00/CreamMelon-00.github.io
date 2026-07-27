# -*- coding: utf-8 -*-
"""storypack.json 직접 주입 CLI — 스튜디오를 거치지 않고 에피소드/배경을 반영한다.

사용법:
  python tools/inject_episode.py 대본.md [--order N] [--subtitle "부제"] [--cover asset_id]
  python tools/inject_episode.py --add-bg resource/bg/bg_A-1.png [--id-name bg_11]
  python tools/inject_episode.py --list
  python tools/inject_episode.py 대본.md --dry-run

동작:
  - md 첫 줄 "N편. 제목" 에서 제목을 읽는다 (전체 줄이 title이 됨).
  - 같은 title의 에피소드가 있으면 script/updatedAt만 갱신 (id 유지 → 방문자 진행도 보존).
  - 없으면 신규 추가. order 기본값은 (기존 최대 order + 1). "N편"의 N은 order로 쓰지 않는다
    (기존 팩이 1편 上/下를 order 1, 2로 나눠 쓰고 있어 편 번호와 order가 일치하지 않음).
  - 저장 전 storypack.json.bak 백업 생성, exportedAt 갱신 (플레이어가 이 값으로 변경을 감지함).
"""
import argparse
import base64
import copy
import json
import re
import secrets
import shutil
import string
import sys
from datetime import datetime, timezone
from pathlib import Path

PACK_PATH = Path(__file__).resolve().parent.parent / "data" / "storypack.json"

TITLE_RE = re.compile(r"^(\d+)편\s*[.:]\s*(.+)$")
BG_CMD_RE = re.compile(r"^@(?:배경|bg|집중|컷신|일러스트|focus|cutscene)\s+(\S+)\s*$", re.IGNORECASE)
# 파이프 없는 화자 줄로 흔히 쓰이는 표현 (스튜디오 문법은 "이름 | 소속"만 화자로 인식)
BARE_SPEAKER_RE = re.compile(r"^(일동|모두|전원)\s*$")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_BASELINE = None


def load_pack():
    global _BASELINE
    if not PACK_PATH.exists():
        sys.exit(f"storypack을 찾을 수 없습니다: {PACK_PATH}")
    with open(PACK_PATH, encoding="utf-8") as f:
        pack = json.load(f)
    # 파일에는 대본이 줄 배열로 저장된다 — 내부에서는 항상 문자열로 다룬다.
    for episode in pack.get("episodes", []):
        if isinstance(episode.get("script"), list):
            episode["script"] = "\n".join(episode["script"])
    _BASELINE = copy.deepcopy(pack)
    return pack


def save_if_changed(pack):
    """실제로 바뀐 게 있을 때만 저장한다.

    exportedAt만 바뀐 커밋은 방문자 전원에게 팩을 다시 내려받게 만들고
    diff도 지저분해지므로, 내용이 같으면 파일을 건드리지 않는다.
    """
    def content(p):
        return {k: v for k, v in p.items() if k != "exportedAt"}

    if _BASELINE is not None and content(pack) == content(_BASELINE):
        print("변경 사항이 없습니다 — storypack을 저장하지 않았습니다.")
        return False
    pack["exportedAt"] = now_iso()
    save_pack(pack)
    return True


def save_pack(pack):
    # 대본을 줄 배열로 풀어서 저장한다. 한 줄짜리 거대 JSON은 git/SourceTree가
    # 글자 단위 비교를 시도하며 멈추고, 무엇이 바뀌었는지도 읽을 수 없다.
    out = copy.deepcopy(pack)
    for episode in out.get("episodes", []):
        if isinstance(episode.get("script"), str):
            episode["script"] = episode["script"].split("\n")

    backup = PACK_PATH.with_suffix(".json.bak")
    shutil.copy2(PACK_PATH, backup)
    tmp = PACK_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(PACK_PATH)
    print(f"저장 완료: {PACK_PATH} (백업: {backup.name})")


META_KEYS = {"부제": "subtitle", "설명": "description", "커버": "cover",
              "챕터": "chapter", "순서": "order", "역할": "role",
              "주역": "focal", "기호": "motif",
              "사건": "event", "날짜": "eventDate"}
ROLE_MAP = {"프롤로그": "prologue", "에필로그": "epilogue",
            "prologue": "prologue", "epilogue": "epilogue"}


def parse_md(md_path):
    """대본 md → (title, script, ep_num, meta).

    첫 비어있지 않은 줄을 제목으로 사용한다. "N편. 제목" 꼴이면 접두어를 떼어
    제목만 남기고 (기존 팩의 제목 관례), N은 order 기본값으로 쓴다.

    제목 바로 다음에 "부제: ...", "커버: ...", "챕터: ...", "순서: ...",
    "설명: ..." 메타 줄을 적을 수 있다 (선택). 본문이 시작되면 더 읽지 않는다.
    """
    text = Path(md_path).read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")

    title = None
    ep_num = None
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip():
            title = line.strip().lstrip("#").strip()
            body_start = i + 1
            break
    if not title:
        sys.exit("대본이 비어 있습니다.")

    m = TITLE_RE.match(title)
    if m:
        ep_num = int(m.group(1))
        title = m.group(2).strip()

    # 제목 뒤 메타 줄 수집
    meta = {}
    i = body_start
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        key, sep, value = s.partition(":")
        if sep and key.strip() in META_KEYS:
            meta[META_KEYS[key.strip()]] = value.strip()
            i += 1
            continue
        break
    body_start = i

    body = []
    for line in lines[body_start:]:
        stripped = line.strip()
        # 스튜디오는 "이름 | 소속" 형식만 화자로 인식 → 파이프 없는 화자 줄 보정
        if BARE_SPEAKER_RE.match(stripped):
            stripped = f"{stripped} |"
        body.append(stripped)

    script = "\n".join(body).strip("\n")
    return title, script, ep_num, meta


def check_assets(script, pack):
    """@배경/@집중 명령이 참조하는 asset id가 팩에 있는지 검사."""
    asset_ids = {a["id"] for a in pack.get("assets", [])}
    missing = []
    for line in script.split("\n"):
        m = BG_CMD_RE.match(line.strip())
        if m and m.group(1) not in asset_ids and m.group(1) != "0":
            missing.append(m.group(1))
    return sorted(set(missing))


def default_chapter(pack):
    chapters = pack.get("meta", {}).get("chapters") or []
    return chapters[0]["key"] if chapters else ""


def ensure_chapter(pack, chapter):
    """meta.chapters에 없는 챕터명이면 자동 등록한다."""
    if not chapter:
        return
    chapters = pack.setdefault("meta", {}).setdefault("chapters", [])
    if any(c.get("key") == chapter for c in chapters):
        return
    order = max((int(c.get("order") or 0) for c in chapters), default=0) + 1
    chapters.append({"key": chapter, "title": chapter, "description": "",
                     "coverAssetId": "", "order": order})
    print(f"새 챕터 등록: {chapter} (order {order})")


def cmd_list(pack):
    print(f"작품: {pack.get('meta', {}).get('title', '?')}  (exportedAt: {pack.get('exportedAt')})")
    episodes = sorted(pack.get("episodes", []),
                      key=lambda e: (int(e.get("order") or 0), e.get("createdAt", "")))
    by_chapter = {}
    for ep in episodes:
        by_chapter.setdefault(ep.get("chapter") or "(미분류)", []).append(ep)
    print(f"\n에피소드 {len(episodes)}개:")
    for chapter, eps in by_chapter.items():
        print(f" 《{chapter}》")
        for ep in eps:
            label = {"prologue": "프롤로그", "epilogue": "에필로그"}.get(ep.get("role"), f"{ep.get('order')}")
            print(f"  [{label}] {ep.get('title')}  — {ep.get('subtitle') or '(부제 없음)'}"
                  f"  (id: {ep.get('id')}, script {len(ep.get('script', ''))}자)")
    print(f"\n배경 에셋 {len(pack.get('assets', []))}개:")
    for a in pack.get("assets", []):
        where = a.get("src") or "내장(base64)"
        print(f"  {a['id']}  ({a.get('fileName')}, {a.get('size', 0) / 1024 / 1024:.1f}MB, {where})")
    print(f"\n용어사전 {len(pack.get('glossary', []))}개")


BG_DIR = PACK_PATH.parent.parent / "resource" / "bg"
DRAFT_DIR = PACK_PATH.parent.parent / "script"
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def cmd_add_bg(pack, image_path, id_name, dry_run):
    """이미지를 resource/bg/ 에 두고 storypack에는 경로(src)만 등록한다."""
    img = Path(image_path)
    if not img.exists():
        sys.exit(f"이미지를 찾을 수 없습니다: {img}")

    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
        img.suffix.lower().lstrip("."))
    if not mime:
        sys.exit(f"지원하지 않는 이미지 형식입니다: {img.suffix}")

    name = id_name or img.stem
    # id 관례: 지역명 기반 슬러그 ("북방주 술집_저녁" → "북방주_술집_저녁"), 중복 시 랜덤 접미사
    asset_id = name.replace(" ", "_")
    if any(a["id"] == asset_id for a in pack.get("assets", [])):
        alphabet = string.ascii_lowercase + string.digits
        asset_id += "_" + "".join(secrets.choice(alphabet) for _ in range(5))

    data = img.read_bytes()
    target = BG_DIR / img.name
    if target.exists() and target.read_bytes() != data:
        target = BG_DIR / f"{name}_{asset_id.rsplit('_', 1)[-1]}{img.suffix.lower()}"

    asset = {
        "id": asset_id,
        "name": name,
        "fileName": target.name,
        "mimeType": mime,
        "size": len(data),
        "createdAt": now_iso(),
        "src": f"resource/bg/{target.name}",
    }
    print(f"배경 추가: {asset_id}  ({target.name}, {len(data) / 1024 / 1024:.1f}MB) → {asset['src']}")
    print(f"대본에서 사용법: @배경 {asset_id}")
    if dry_run:
        print("(dry-run: 저장하지 않음)")
        return
    if img.resolve() != target.resolve():
        BG_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    pack["assets"].append(asset)
    save_if_changed(pack)


def cmd_externalize(pack, dry_run):
    """base64로 내장된 배경들을 resource/bg/ 파일로 추출하고 src 참조로 바꾼다."""
    changed = 0
    for a in pack.get("assets", []):
        data_url = a.get("dataUrl")
        if not data_url:
            continue
        ext = MIME_EXT.get(a.get("mimeType"), ".png")
        data = base64.b64decode(data_url.partition(",")[2])
        file_name = a.get("fileName") or f"{a['id']}{ext}"
        target = BG_DIR / file_name
        if target.exists() and target.read_bytes() != data:
            target = BG_DIR / f"{a['id']}{ext}"
        print(f"  {a['id']}: {len(data) / 1024 / 1024:.1f}MB → resource/bg/{target.name}")
        if not dry_run:
            BG_DIR.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            a["src"] = f"resource/bg/{target.name}"
            a["fileName"] = target.name
            del a["dataUrl"]
        changed += 1
    if not changed:
        print("내장(base64) 배경이 없습니다 — 변환할 것이 없습니다.")
        return
    if dry_run:
        print(f"(dry-run: {changed}개 변환 예정, 저장하지 않음)")
        return
    save_if_changed(pack)
    print(f"완료: 배경 {changed}개 분리")


def cmd_inject(pack, md_path, order, subtitle, cover, description, chapter, dry_run,
               save=True):
    title, script, ep_num, md_meta = parse_md(md_path)

    # 우선순위: CLI 플래그 > md 메타 줄 > 기존 값 유지
    if subtitle is None:
        subtitle = md_meta.get("subtitle")
    if cover is None:
        cover = md_meta.get("cover")
    if description is None:
        description = md_meta.get("description")
    if chapter is None:
        chapter = md_meta.get("chapter")
    if order is None and "order" in md_meta:
        try:
            order = int(md_meta["order"])
        except ValueError:
            print(f"경고: 순서 값이 숫자가 아닙니다: {md_meta['order']}")

    role = None
    if "role" in md_meta:
        role = ROLE_MAP.get(md_meta["role"].strip())
        if role is None:
            print(f"경고: 알 수 없는 역할 값: {md_meta['role']} (프롤로그/에필로그만 가능)")
        elif order is None:
            # 역할 에피소드의 순서는 정렬용 관례값 (플레이어는 role로 정렬)
            order = 0 if role == "prologue" else 99

    missing = check_assets(script, pack)
    if missing:
        print(f"경고: 팩에 없는 배경 id 참조 {len(missing)}건 → {', '.join(missing)}")
        print("  (--add-bg 로 배경을 먼저 추가하거나, 대본의 @배경 id를 확인하세요. 주입은 계속 진행합니다.)")

    episodes = pack.setdefault("episodes", [])
    existing = next((e for e in episodes if e.get("title") == title), None)
    now = now_iso()

    if existing:
        action = "갱신"
        before = copy.deepcopy(existing)
        existing["script"] = script
        if order is not None:
            existing["order"] = order
        if subtitle is not None:
            existing["subtitle"] = subtitle
        if cover is not None:
            existing["coverAssetId"] = cover
        if description is not None:
            existing["description"] = description
        if chapter is not None:
            existing["chapter"] = chapter
        if role is not None:
            existing["role"] = role
        if "focal" in md_meta:
            existing["focal"] = md_meta["focal"]
        if "event" in md_meta:
            existing["event"] = md_meta["event"]
        if "eventDate" in md_meta:
            existing["eventDate"] = md_meta["eventDate"]
        # 내용이 그대로면 updatedAt도 건드리지 않는다 (배포마다 diff가 지저분해지는 것 방지)
        if existing == before:
            action = "변경 없음"
        else:
            existing["updatedAt"] = now
        target = existing
    else:
        action = "신규 추가"
        if order is None:
            order = ep_num if ep_num else max((int(e.get("order") or 0) for e in episodes), default=0) + 1
        clash = next((e for e in episodes if int(e.get("order") or 0) == order), None)
        if clash:
            print(f"경고: order {order}는 이미 '{clash.get('title')}'가 사용 중입니다. "
                  f"같은 order는 등록순으로 정렬됩니다.")
        target = {
            "id": "episode_" + secrets.token_hex(6),
            "order": order,
            "chapter": chapter if chapter is not None else default_chapter(pack),
            **({"role": role} if role else {}),
            **({"focal": md_meta["focal"]} if "focal" in md_meta else {}),
            **({"event": md_meta["event"]} if "event" in md_meta else {}),
            **({"eventDate": md_meta["eventDate"]} if "eventDate" in md_meta else {}),
            "title": title,
            "subtitle": subtitle or "",
            "description": description or "",
            "coverAssetId": cover or "",
            "script": script,
            "createdAt": now,
            "updatedAt": now,
        }
        episodes.append(target)

    ensure_chapter(pack, target.get("chapter"))

    # 주역 인물의 기호를 팩 공용 매핑(meta.motifs)에 등록
    if "motif" in md_meta and target.get("focal"):
        motifs = pack.setdefault("meta", {}).setdefault("motifs", {})
        if motifs.get(target["focal"]) != md_meta["motif"]:
            motifs[target["focal"]] = md_meta["motif"]
            print(f"기호 등록: {target['focal']} → {md_meta['motif']}")

    speakers = sorted({line.split("|")[0].strip() for line in script.split("\n")
                       if "|" in line and not line.startswith(("(", "@", "-"))})
    print(f"{action}: [{target['order']}] {title}  (챕터: {target.get('chapter') or '미분류'})")
    print(f"  script {len(script)}자, 화자: {', '.join(speakers) or '(없음)'}")
    if dry_run:
        print("(dry-run: 저장하지 않음)")
        return
    if save:
        save_if_changed(pack)


def cmd_inject_all(pack, dry_run):
    """script 폴더(하위 폴더 포함)의 모든 md를 반영하고 한 번만 저장한다."""
    md_files = sorted(DRAFT_DIR.rglob("*.md"),
                      key=lambda p: (str(p.parent), len(p.stem.split("편")[0]), p.stem))
    if not md_files:
        sys.exit(f"드래프트 폴더에 md가 없습니다: {DRAFT_DIR}")
    for md in md_files:
        cmd_inject(pack, md, None, None, None, None, None, dry_run, save=False)
    if dry_run:
        print(f"\n(dry-run: {len(md_files)}개 파일 확인만 함)")
        return
    save_if_changed(pack)
    print(f"\n총 {len(md_files)}개 대본 반영 완료")


def main():
    ap = argparse.ArgumentParser(description="storypack.json 에피소드/배경 주입 CLI")
    ap.add_argument("md", nargs="?", help="주입할 대본 md 파일")
    ap.add_argument("--order", type=int, help="에피소드 순서 (기본: 기존 최대+1, 갱신 시 유지)")
    ap.add_argument("--chapter", help="챕터명 (신규 기본: 팩의 첫 챕터, 갱신 시 유지)")
    ap.add_argument("--subtitle", help="부제")
    ap.add_argument("--description", help="설명")
    ap.add_argument("--cover", help="커버 배경 asset id")
    ap.add_argument("--add-bg", metavar="IMAGE", help="배경 이미지를 에셋으로 추가 (resource/bg 경로 참조)")
    ap.add_argument("--id-name", help="--add-bg 시 에셋 이름 (기본: 파일명)")
    ap.add_argument("--all", action="store_true",
                    help="드래프트 폴더의 모든 md를 한 번에 반영")
    ap.add_argument("--externalize", action="store_true",
                    help="팩에 base64로 내장된 배경들을 resource/bg/ 파일로 분리")
    ap.add_argument("--list", action="store_true", help="현재 팩의 에피소드/에셋 목록 출력")
    ap.add_argument("--dry-run", action="store_true", help="변경 내용만 출력하고 저장하지 않음")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    pack = load_pack()

    if args.list:
        cmd_list(pack)
    elif args.all:
        cmd_inject_all(pack, args.dry_run)
    elif args.externalize:
        cmd_externalize(pack, args.dry_run)
    elif args.add_bg:
        cmd_add_bg(pack, args.add_bg, args.id_name, args.dry_run)
    elif args.md:
        cmd_inject(pack, args.md, args.order, args.subtitle, args.cover, args.description,
                   args.chapter, args.dry_run)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
