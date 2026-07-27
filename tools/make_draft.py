# -*- coding: utf-8 -*-
"""추출 txt(페이지 범위) → 초벌 대본 md 생성기.

사용법:
  python tools/make_draft.py --source main --pages 2-14 --title "1편. 아버지의 그림자 上"
  python tools/make_draft.py --source re --pages 1-20 --title "..." --out 경로.md

동작:
  1. 알려진 (이름, 역할) 사전을 수집
     - storypack.json 안의 기존 대본 화자 라벨
     - Desktop/Story 의 *.md 대본 화자 라벨
     - 추출 전체에서 "짧은 역할 줄" 앞에 반복 등장하는 이름 후보 (빈도 기반)
  2. 지정한 페이지의 txt를 이어붙이고, 대사 꼬리에 붙은 화자 라벨을 분리해
     "이름 | 역할" 선행 형식으로 재배치
  3. pypdf가 벌려놓은 공백(단어 사이 이중 공백, 문장부호 앞 공백)을 정리
  4. 확신이 없는 분리 지점에는 <<확인>> 마커를 남긴다 (수동 손질 대상)

추출 레이아웃 특성 (이 스크립트가 다루는 것들):
  - 화자 이름이 직전 대사 줄의 꼬리에 붙는다:  "...괜찮나?   네빌"
  - 역할이 다음 줄(들)에 온다. 두 단어 역할은 줄이 쪼개진다: "하노버" / "용병단"
  - 긴 대사는 한 단어씩 줄바꿈된다 → 화자 전환은 문장이 끝난 지점에서만 허용해 걸러낸다
  - 결정적 신호: 화자 이름이 든 줄은 항상 꼬리 공백으로 끝난다 ('앤드루  ').
    단어 줄바꿈 조각('가문의')은 꼬리 공백이 없다 → 이 구조로 둘을 구분한다
  - 문장부호만 있는 줄("…?")은 직전 조각에 붙인다

목표는 100% 자동화가 아니라 "초벌 80% + 손질 20%"이다.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

EXTRACT_DIR = Path("C:/Users/Superplanet/Desktop/Story/_extract")
STORY_DIR = Path("C:/Users/Superplanet/Desktop/Story")
PACK_PATH = Path(__file__).resolve().parent.parent / "data" / "storypack.json"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "script"

PREFIX = {"main": "main_p", "ex": "ex_p", "re": "re_p", "char": "char_p"}

# 대사가 끝났다고 볼 수 있는 꼬리 문자들 (이 뒤에 오는 토큰이 화자 이름 후보)
# 따옴표는 제외한다: 문장 중간의 ‘인용구’ 뒤 단어를 화자로 오인하게 만든다
# "*"는 행동 표기 꼬리("*큭큭*"), "—–"는 말이 끊기는 연출("아직 작업은 —")용
SENT_END = ".?!…~)】]』」》*—–"
PUNCT_ONLY_RE = re.compile(r"^[.?!…,~]+$")
CHECK = "<<확인>>"
MAX_ROLE_PARTS = 3


# ---------------------------------------------------------------- 공백 정리

def fix_spacing(text):
    """pypdf 추출 특유의 벌어진 공백을 자연스러운 한국어 표기로 정리."""
    t = re.sub(r"[ \t ]+", " ", text).strip()
    t = re.sub(r"(\d+) 편\s*\.", r"\1편.", t)
    # 문장부호 앞 공백 제거
    t = re.sub(r" +([.,?!…~])", r"\1", t)
    # 닫는 괄호/따옴표 앞, 여는 괄호/따옴표 뒤 공백 제거
    t = re.sub(r" +([)\]』」》’”])", r"\1", t)
    t = re.sub(r"([(\[『「《‘“]) +", r"\1", t)
    # 행동 표기 "* 소근 *" → "*소근*"
    t = re.sub(r"\*\s*([^*]*?)\s*\*", lambda m: f"*{m.group(1)}*", t)
    # 숫자와 단위 사이 공백 제거: "12 번째" → "12번째"
    t = re.sub(r"(\d) +(?=번째|번|시간|시|분|초|명|개|년|월|일|살|층|호)", r"\1", t)
    # 줄 시작의 말줄임표는 뒤 단어에 붙인다: "… 아무튼" → "…아무튼"
    t = re.sub(r"^([…]+) +", r"\1", t)
    # 닫는 따옴표 뒤 조사는 붙인다: "‘척’ 만" → "‘척’만"
    t = re.sub(r"([’”]) +(이|가|은|는|을|를|과|와|도|만|의|에|에서|으로|로|이지|이랑|랑"
               r"|이라|한테|께서|처럼|보다|부터|까지)(?=[ .,?!…~]|$)", r"\1\2", t)
    return t


def ends_sentence(text):
    return bool(text) and text.rstrip()[-1] in SENT_END


# ---------------------------------------------------------------- 사전 수집

def harvest_known_pairs():
    """(이름, 역할) 확정 사전을 기존 완성 대본에서 수집한다."""
    pairs = set()

    def from_script(script):
        for line in script.split("\n"):
            line = line.strip()
            if "|" in line and not line.startswith(("(", "@", "-", "[")):
                name, _, role = line.partition("|")
                name, role = name.strip(), role.strip()
                if name and len(name) <= 20:
                    pairs.add((name, role))

    if PACK_PATH.exists():
        with open(PACK_PATH, encoding="utf-8") as f:
            pack = json.load(f)
        for ep in pack.get("episodes", []):
            script = ep.get("script", "")
            # 팩에는 대본이 줄 배열로 저장되어 있다 (git diff 가독성)
            from_script("\n".join(script) if isinstance(script, list) else script)

    for md in STORY_DIR.glob("*.md"):
        from_script(md.read_text(encoding="utf-8"))

    return pairs


def harvest_from_extract():
    """추출 전체를 훑어 (이름, 역할) 후보를 빈도 기반으로 수집한다.

    패턴: 문장이 끝난 줄의 꼬리 토큰(이름) + 다음 줄(들)이 짧은 역할 줄.
    두 줄짜리 역할("하노버"/"용병단")도 조합으로 함께 센다.
    main/expansion PDF에 같은 내용이 중복 수록되어 있어 노이즈도 2회씩 잡히므로,
    3회 이상 반복된 조합만 채택한다.
    """
    counter = Counter()
    for txt in EXTRACT_DIR.glob("*_p*.txt"):
        try:
            raw = [l.rstrip("\r") for l in txt.read_text(encoding="utf-8").split("\n")]
        except UnicodeDecodeError:
            continue
        raw = [l for l in raw if l.strip()]
        lines = [l.strip() for l in raw]  # 압축 뷰 (raw와 인덱스 일치)
        for i in range(len(lines) - 1):
            line = lines[i]
            if not is_role_like(lines[i + 1]):
                continue
            # 이름 줄은 꼬리 공백으로 끝난다 (원본 줄 기준)
            if not raw[i].endswith((" ", "\t")):
                continue
            tail = line.rstrip("|").strip()
            tokens = [t for t in tail.split(" ") if t]
            if not tokens:
                continue
            # 역할 후보: 다음 줄 하나 / 다음 두 줄 조합
            role_cands = [fix_spacing(lines[i + 1])]
            if i + 2 < len(lines) and is_role_like(lines[i + 2]):
                role_cands.append(fix_spacing(lines[i + 1] + " " + lines[i + 2]))
            for take in (1, 2):
                if len(tokens) < take:
                    break
                cand = " ".join(tokens[-take:])
                rest = tokens[:-take]
                before = rest[-1] if rest else ""
                if before:
                    if not ends_sentence(before):
                        continue
                else:
                    prev = lines[i - 1] if i > 0 else ""
                    if prev and not ends_sentence(prev):
                        continue
                if is_name_like(cand):
                    for role in role_cands:
                        counter[(cand, role)] += 1
    return {pair for pair, n in counter.items() if n >= 3}


def is_role_like(line):
    """역할 줄 후보: 짧고, 문장부호가 없으며, 명령/괄호 줄이 아니다."""
    s = line.strip()
    if not s or len(s) > 16:
        return False
    if any(ch in s for ch in ".!…,,~()[]{}@<>「」『』《》*"):
        return False
    if "?" in s and s != "???":
        return False
    if s.startswith(("-", "–", "|")):
        return False
    return True


def is_name_like(s):
    s = s.strip()
    if not s or len(s) > 12:
        return False
    if s in ("–", "-", "|"):
        return False
    # "???"는 화자로 쓰이지만 "?" 단독은 문장부호다
    if set(s) <= set("?.!…,~"):
        return s == "???"
    if any(ch in s for ch in ".!…,~()[]{}@<>*"):
        return False
    return True


# ---------------------------------------------------------------- 본문 변환

class Converter:
    def __init__(self, known_pairs):
        self.known_pairs = set(known_pairs)
        self.known_names = {p[0] for p in known_pairs}
        self.known_roles = {p[1] for p in known_pairs}
        self.out = []          # 출력 줄 목록
        self.speaker = None    # (이름, 역할)
        self.buffer = []       # 현재 화자의 대사 조각
        self.check_count = 0
        self.dialogue_count = 0
        self.seen_speakers = set()

    # ---- 출력 도우미
    def flush(self):
        text = fix_spacing(" ".join(self.buffer))
        self.buffer = []
        if not text:
            return
        if self.speaker:
            name, role = self.speaker
            self.out.append(f"{name} | {role}")
            self.out.append(text)
            self.out.append("")
            self.dialogue_count += 1
            self.seen_speakers.add(name)
        else:
            self.out.append(text)
            self.out.append("")

    def emit(self, line):
        self.flush()
        self.out.append(line)
        self.out.append("")

    def buffer_open(self):
        """현재 버퍼가 문장 중간인지 (중간이면 화자 전환 불가)"""
        text = " ".join(self.buffer).rstrip()
        return bool(text) and not ends_sentence(text)

    def learn(self, name, role):
        self.known_pairs.add((name, role))
        self.known_names.add(name)
        self.known_roles.add(role)

    # ---- 메인 루프
    def feed_pages(self, pages_lines, skip_lines):
        lines = []
        for pl in pages_lines:
            lines.extend(pl)
        # 변형 레이아웃: 이름/|/역할이 각각 딴 줄로 쪼개진 경우
        # → "|"를 직전 내용 줄에 합친다 (사이에 빈 줄이 끼어 있을 수 있다)
        merged = []
        for l in lines:
            if l.strip() == "|" and any(m.strip() for m in merged):
                k = len(merged) - 1
                while not merged[k].strip():
                    k -= 1
                merged[k] = merged[k].rstrip() + " |  "
            else:
                merged.append(l)
        lines = merged
        i = 0
        while i < len(lines):
            line = lines[i].strip()

            if not line or fix_spacing(line) in skip_lines:
                i += 1
                continue

            # 문장부호 단독 줄 → 직전 조각에 붙인다 (화자 판정보다 먼저)
            if PUNCT_ONLY_RE.match(line):
                if self.buffer:
                    self.buffer[-1] = self.buffer[-1].rstrip() + line
                i += 1
                continue

            # 씬 구분자 (en-dash 단독)
            if line in ("–", "—"):
                self.flush()
                self.speaker = None
                self.out.append("–")
                self.out.append("")
                i += 1
                continue

            # 편 제목 줄 (편 경계는 페이지 중간에 올 수 있다) → 구분 마커로 출력
            if re.match(r"^\d+\s*편\s*\.", line):
                self.flush()
                self.speaker = None
                self.out.append(f"## {fix_spacing(line)}")
                self.out.append("")
                i += 1
                continue

            # 시간 표시 등 <...> 줄
            if re.match(r"^<.+>$", line):
                self.flush()
                self.speaker = None
                self.out.append(fix_spacing(line))
                self.out.append("")
                i += 1
                continue

            # 씬 헤더: "- 장소" (꼬리에 다음 화자 이름이 붙어있을 수 있음)
            if re.match(r"^-\s+", line):
                i = self.consume_scene_header(line, lines, i)
                continue

            # 화자 라벨: 이 줄 꼬리가 이름이고 다음 줄이 역할이면 분리.
            # 이름이 든 줄은 원본에서 항상 꼬리 공백으로 끝난다 — 단어 줄바꿈
            # 조각('가문의')은 꼬리 공백이 없어 여기서 걸러진다.
            if lines[i].rstrip("\r").endswith((" ", "\t")):
                advanced = self.consume_speaker_tail(line, lines, i)
                if advanced is not None:
                    i = advanced
                    continue

            # 그 외: 대사/나레이션 조각
            self.consume_fragment(line)
            i += 1

        self.flush()

    # ---- 부분 해석
    def next_nonempty(self, lines, i):
        return next((j for j in range(i + 1, len(lines)) if lines[j].strip()), None)

    def absorb_role(self, lines, start_idx):
        """start_idx부터 역할 줄(들)을 흡수해 (역할, 다음 인덱스)를 돌려준다.

        두 단어 역할은 줄이 쪼개져 있으므로, 알려진 역할 사전과 대조해
        가장 긴 조합을 선택한다. 사전에 없으면 첫 줄만 역할로 본다.
        """
        parts = []
        idxs = []
        j = start_idx
        while len(parts) < MAX_ROLE_PARTS and j is not None and j < len(lines):
            s = lines[j].strip()
            if not s:
                j += 1
                continue
            if not is_role_like(s):
                break
            parts.append(fix_spacing(s))
            idxs.append(j)
            j += 1
        if not parts:
            return None, start_idx

        best = parts[0]
        best_end = idxs[0] + 1
        for k in range(len(parts), 1, -1):
            combined = " ".join(parts[:k])
            if combined in self.known_roles:
                best = combined
                best_end = idxs[k - 1] + 1
                break
        return best, best_end

    def match_speaker(self, tokens, role_line, has_pipe, mid_sentence,
                      require_known=False, relax_boundary=False):
        """토큰 꼬리에서 (이름, 남은 대사, 확신도)를 찾는다. 실패 시 None.

        1토큰 이름을 먼저 시도한다 — 2토큰을 먼저 잡으면 장소/대사 꼬리가
        이름에 딸려 들어가는 과확장이 생긴다 ("저택 앤드루").
        "|"가 있으면 마지막 문장부호 뒤 전체를 이름으로 본다 ("연방 의원 A |"
        같은 3토큰 이름 대응). 3토큰은 그 외엔 사전 일치로만 인정한다.
        """
        if has_pipe:
            split_at = -1
            for idx, token in enumerate(tokens):
                if ends_sentence(token):
                    split_at = idx
            cand = " ".join(tokens[split_at + 1:])
            rest = tokens[:split_at + 1]
            if cand and is_name_like(cand) and not (not rest and mid_sentence):
                known = ((cand, role_line) in self.known_pairs
                         or (cand in self.known_names and role_line in self.known_roles))
                if known:
                    return cand, rest, "known"
                if not require_known:
                    return cand, rest, "pipe"

        for take in (1, 2, 3):
            if len(tokens) < take:
                continue
            cand = " ".join(tokens[-take:])
            rest = tokens[:-take]
            before = rest[-1] if rest else ""
            # "|"가 있으면 쉼표로 끊긴 대사 뒤의 화자도 인정한다 ("...들일 뿐,  앤드루 |")
            boundary_ok = (relax_boundary or (not before) or ends_sentence(before)
                           or (has_pipe and before[-1] == ","))
            if not boundary_ok:
                continue
            if not rest and mid_sentence:
                continue

            known = ((cand, role_line) in self.known_pairs
                     or (cand in self.known_names and role_line in self.known_roles))
            if known:
                return cand, rest, "known"
            if take >= 3 or require_known:
                continue
            if has_pipe and is_name_like(cand):
                return cand, rest, "pipe"
            # 사전에 없는 새 화자 추정 (꼬리 공백 구조는 호출부에서 이미 확인됨).
            # 처음 등장하는 인물을 잡기 위한 것으로, <<확인>> 마커가 붙는다.
            if not rest and is_name_like(cand) and not mid_sentence:
                return cand, rest, "guess"
        return None

    def set_speaker(self, cand, role_line, confidence):
        self.speaker = (cand, role_line)
        if confidence == "guess" and (cand, role_line) not in self.known_pairs:
            self.out.append(f"{CHECK} 새 화자로 추정 → 아래 라벨 확인")
            self.check_count += 1
        self.learn(cand, role_line)

    def consume_speaker_tail(self, line, lines, i):
        nxt_idx = self.next_nonempty(lines, i)
        if nxt_idx is None:
            return None
        role_line, role_end = self.absorb_role(lines, nxt_idx)
        if role_line is None:
            return None

        tail = line.rstrip()
        has_pipe = tail.endswith("|")
        tokens = [t for t in tail.rstrip("|").strip().split(" ") if t]
        if not tokens:
            return None

        found = self.match_speaker(tokens, role_line, has_pipe, self.buffer_open())
        if not found:
            return None
        cand, rest, confidence = found

        if rest:
            self.consume_fragment(" ".join(rest))
        self.flush()
        self.set_speaker(cand, role_line, confidence)
        return role_end

    def consume_scene_header(self, line, lines, i):
        self.flush()
        self.speaker = None
        body = re.sub(r"^-\s+", "", line).strip()

        # 헤더에 붙은 <시간 표시>는 별도 줄로 분리한다
        time_cues = [fix_spacing(m) for m in re.findall(r"<[^<>]+>", body)]
        if time_cues:
            body = re.sub(r"\s*<[^<>]+>\s*", " ", body).strip()

        nxt_idx = self.next_nonempty(lines, i)
        if nxt_idx is not None:
            role_line, role_end = self.absorb_role(lines, nxt_idx)
            if role_line is not None:
                tokens = [t for t in body.rstrip("|").strip().split(" ") if t]
                # 장소명은 문장이 아니므로 경계 검사를 풀되, 사전 일치만 인정
                found = self.match_speaker(tokens, role_line, body.rstrip().endswith("|"),
                                           False, require_known=True, relax_boundary=True)
                if found:
                    cand, rest, confidence = found
                    self.emit_scene_header(" ".join(rest), time_cues)
                    self.set_speaker(cand, role_line, confidence)
                    return role_end

        self.emit_scene_header(body, time_cues)
        return i + 1

    def emit_scene_header(self, body, time_cues):
        self.out.append(f"- {fix_spacing(body)}")
        self.out.append("")
        for cue in time_cues:
            self.out.append(cue)
            self.out.append("")

    def consume_fragment(self, text):
        """대사/나레이션 조각을 버퍼에 쌓는다. 효과음 괄호·씬 구분 꼬리는 분리."""
        text = text.strip()
        scene_break = False
        if text.endswith(("–", "—")):
            stripped = text[:-1].rstrip()
            # 문장이 끝난 뒤의 대시만 씬 구분자다.
            # 문장 중간이면 말이 끊기는 연출("아직 작업은 —")이므로 대사에 남긴다.
            if not stripped or ends_sentence(stripped):
                text = stripped
                scene_break = True

        # 조각 꼬리의 효과음 괄호를 별도 줄로 분리: "저 새끼들이…! ( 파열음 )"
        trailing_sfx = None
        m = re.match(r"^(.*?)\(\s*([^()]{1,20})\s*\)$", text)
        if m and (not m.group(1) or ends_sentence(m.group(1))):
            text = m.group(1).strip()
            trailing_sfx = fix_spacing(m.group(2))

        if text:
            if self.buffer and PUNCT_ONLY_RE.match(text):
                self.buffer[-1] = self.buffer[-1].rstrip() + text
            else:
                self.buffer.append(text)

        if trailing_sfx is not None:
            self.emit(f"({trailing_sfx})")
        if scene_break:
            self.flush()
            self.speaker = None
            self.out.append("–")
            self.out.append("")


# ---------------------------------------------------------------- 메인

def parse_pages(spec):
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages


def main():
    ap = argparse.ArgumentParser(description="추출 txt → 초벌 대본 md 생성기")
    ap.add_argument("--source", choices=PREFIX, required=True, help="원본 PDF 종류")
    ap.add_argument("--pages", required=True, help="페이지 범위 (예: 2-14 또는 2-5,8)")
    ap.add_argument("--title", required=True, help="대본 제목 (예: '1편. 아버지의 그림자 上')")
    ap.add_argument("--out", help="출력 경로 (기본: StoryProject/script/제목.md)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    pages = parse_pages(args.pages)
    prefix = PREFIX[args.source]

    pages_lines = []
    for p in pages:
        path = EXTRACT_DIR / f"{prefix}{p:03d}.txt"
        if not path.exists():
            sys.exit(f"추출 파일이 없습니다: {path}")
        pages_lines.append(path.read_text(encoding="utf-8").split("\n"))

    known = harvest_known_pairs()
    known |= harvest_from_extract()

    # 매 페이지 반복되는 작품 제목 줄은 건너뛴다
    skip_lines = set()
    if PACK_PATH.exists():
        with open(PACK_PATH, encoding="utf-8") as f:
            title = json.load(f).get("meta", {}).get("title", "")
        if title:
            skip_lines.add(title)

    conv = Converter(known)
    conv.feed_pages(pages_lines, skip_lines)

    # 원본의 씬 구분자(–)를 현행 문법으로 변환:
    # 바로 뒤에 씬 헤더/@배경/<시간 표시>가 오면 불필요하므로 제거,
    # 장면 안의 시간 경과 표시면 @정적 1.5 로 대체한다.
    processed = []
    for i, line in enumerate(conv.out):
        if line.strip() in ("–", "—", "--"):
            nxt = next((l.strip() for l in conv.out[i + 1:] if l.strip()), "")
            if (not nxt or nxt.startswith("- ") or nxt.startswith("@배경")
                    or (nxt.startswith("<") and nxt.endswith(">")) or nxt.startswith("##")):
                continue
            processed.append("@정적 1.5")
        else:
            processed.append(line)
    body = "\n".join(processed)
    body = re.sub(r"\n{3,}", "\n\n", body).strip("\n")

    out_path = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"{args.title}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(f"{args.title}\n\n\n{body}\n", encoding="utf-8")

    speakers = sorted(conv.seen_speakers)
    print(f"생성 완료: {out_path}")
    print(f"  페이지 {len(pages)}장 → 대사 {conv.dialogue_count}개, 화자 {len(speakers)}명: {', '.join(speakers)}")
    print(f"  {CHECK} 마커 {conv.check_count}곳 (사전에 없던 새 화자 추정 지점 — 검토 필요)")


if __name__ == "__main__":
    main()
