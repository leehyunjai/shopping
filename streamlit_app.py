"""쇼핑 리스트 — Streamlit 버전.

웹 버전(index.html / script.js)과 같은 기능과 데이터 형식을 쓴다.
브라우저 localStorage 대신 같은 폴더의 shopping_list.json 파일에 저장한다.

실행: streamlit run streamlit_app.py
"""

import hashlib
import html
import json
import math
import time
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

DATA_FILE = Path(__file__).with_name("shopping_list.json")
MAX_NAME_LENGTH = 100
UNDO_TIMEOUT_SEC = 5
# 한 화면에 그리는 아이템 수. Streamlit은 동작마다 화면 전체를 다시 그리므로
# 500개를 한 번에 그리면 동작 한 번에 2~3초가 걸린다.
PAGE_SIZE = 50

# 아이템 형태: {"id": str, "name": str, "checked": bool, "createdAt": int(ms)}


def normalize_name(value) -> str:
    """앞뒤 공백을 지우고 최대 길이로 자른 이름. 비어 있으면 빈 문자열."""
    return str(value).strip()[:MAX_NAME_LENGTH]


def widget_key(item_id: str) -> str:
    """위젯 key·CSS 클래스에 쓸 수 있는 안전한 문자열 (id는 파일에서 온 임의 문자열일 수 있다)."""
    return hashlib.sha1(item_id.encode("utf-8")).hexdigest()[:12]


# ---------- 파일 저장소 ----------


def backup_corrupt_file() -> None:
    """읽을 수 없는 데이터 파일을 덮어쓰기 전에 옆으로 옮겨 둔다."""
    backup = DATA_FILE.with_name(f"shopping_list.corrupt-{datetime.now():%Y%m%d-%H%M%S}.json")
    try:
        DATA_FILE.replace(backup)
        st.session_state.corrupt_backup = backup.name
    except OSError:
        pass


def load_items() -> list[dict]:
    """파일에서 목록을 불러온다. 형식이 잘못된 항목은 걸러내고, 파일 전체가 깨졌으면 백업한다."""
    try:
        # utf-8-sig: 메모장 등이 붙이는 BOM이 있어도 읽는다
        text = DATA_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        backup_corrupt_file()
        return []
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if not isinstance(parsed, list):
        backup_corrupt_file()
        return []

    items, seen_ids = [], set()
    for item in parsed:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("name"), str)
            and normalize_name(item["name"])
        ):
            continue
        if item["id"] in seen_ids:
            continue
        seen_ids.add(item["id"])
        created_at = item.get("createdAt")
        valid_created = isinstance(created_at, (int, float)) and not isinstance(created_at, bool)
        items.append({
            "id": item["id"],
            "name": normalize_name(item["name"]),
            "checked": item.get("checked") is True,
            "createdAt": created_at if valid_created else int(time.time() * 1000),
        })
    return items


def save_items(items: list[dict]) -> None:
    """임시 파일에 쓴 뒤 교체해서, 저장 중에 끊겨도 파일이 깨지지 않게 한다."""
    try:
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DATA_FILE)
    except OSError:
        st.session_state.save_error = True


def mutate(change):
    """파일에서 최신 목록을 다시 읽어 변경을 적용하고 저장한다.

    창(세션)마다 처음 읽은 목록을 그대로 덮어쓰면, 다른 창에서 한 변경이 사라진다.
    """
    items = load_items()
    result = change(items)
    save_items(items)
    st.session_state.shopping_items = items
    return result


def find_index(items: list[dict], item_id: str) -> int:
    return next((i for i, item in enumerate(items) if item["id"] == item_id), -1)


def request_focus(kind: str, item_id: str | None = None, keyboard_only: bool = True) -> None:
    """다음 화면에서 키보드 포커스를 옮길 대상. 실제 이동은 아래 JavaScript가 한다."""
    st.session_state.focus = {
        "kind": kind,
        "h": widget_key(item_id) if item_id else None,
        "keyboardOnly": keyboard_only,
    }


# ---------- 상태 변경 (모두 위젯 콜백에서 호출) ----------


def add_item() -> None:
    name = normalize_name(st.session_state.new_item)
    if not name:
        return
    new_item = {"id": str(uuid.uuid4()), "name": name, "checked": False, "createdAt": int(time.time() * 1000)}

    def change(items):
        items.append(new_item)
        return len(items) - 1

    index = mutate(change)
    # 새 아이템이 있는 페이지로 이동하고, 화면에 보이도록 스크롤한다
    st.session_state.page = index // PAGE_SIZE
    request_focus("added", new_item["id"], keyboard_only=False)


def toggle_item(item_id: str, key: str) -> None:
    # 뒤집지 않고 위젯 값을 그대로 쓴다 (다른 창에서 이미 바꿨어도 결과가 맞도록)
    checked = bool(st.session_state.get(key))

    def change(items):
        index = find_index(items, item_id)
        if index != -1:
            items[index]["checked"] = checked

    mutate(change)
    request_focus("check", item_id)


def start_edit(item_id: str) -> None:
    # 다른 아이템을 편집 중이었다면 그 편집은 취소된다
    st.session_state.editing_id = item_id
    request_focus("edit", item_id, keyboard_only=False)


def save_edit(item_id: str) -> None:
    name = normalize_name(st.session_state.get(f"edit_{widget_key(item_id)}", ""))

    def change(items):
        index = find_index(items, item_id)
        # 빈 값이면 원래 이름을 유지한 채 편집만 끝낸다
        if index != -1 and name:
            items[index]["name"] = name

    mutate(change)
    st.session_state.editing_id = None
    request_focus("editbtn", item_id)


def cancel_edit(item_id: str) -> None:
    st.session_state.editing_id = None
    request_focus("editbtn", item_id)


def set_undo(entries: list[dict], message: str) -> None:
    st.session_state.last_deleted = {"entries": entries, "message": message, "at": time.time()}
    request_focus("undo")


def delete_item(item_id: str) -> None:
    def change(items):
        index = find_index(items, item_id)
        return (items.pop(index), index) if index != -1 else None

    result = mutate(change)
    if st.session_state.editing_id == item_id:
        st.session_state.editing_id = None
    if result:
        removed, index = result
        set_undo([{"item": removed, "index": index}], f"'{removed['name']}' 삭제됨")


def clear_checked() -> None:
    def change(items):
        removed = [{"item": item, "index": i} for i, item in enumerate(items) if item["checked"]]
        items[:] = [item for item in items if not item["checked"]]
        return removed

    removed = mutate(change)
    if any(e["item"]["id"] == st.session_state.editing_id for e in removed):
        st.session_state.editing_id = None
    if removed:
        set_undo(removed, f"체크한 항목 {len(removed)}개 삭제됨")


def undo_delete() -> None:
    pending = st.session_state.last_deleted
    if not pending:
        return
    st.session_state.last_deleted = None

    def change(items):
        existing = {item["id"] for item in items}
        restored = []
        # 원래 위치가 앞쪽인 것부터 넣어야 각 index가 그대로 맞는다
        for entry in sorted(pending["entries"], key=lambda e: e["index"]):
            if entry["item"]["id"] in existing:  # 다른 창에서 이미 되돌린 경우
                continue
            index = min(entry["index"], len(items))
            items.insert(index, entry["item"])
            restored.append((entry["item"]["id"], index))
        return restored

    restored = mutate(change)
    if restored:
        first_id, first_index = restored[0]
        st.session_state.page = first_index // PAGE_SIZE
        request_focus("check", first_id)


def go_page(delta: int) -> None:
    st.session_state.page += delta


# ---------- 화면 ----------

# Streamlit은 폭 640px 미만에서 열(columns)을 세로로 쌓는다.
# 아이템 한 줄(체크박스·이름·수정·삭제)이 모바일에서도 가로로 유지되도록 막는다.
CUSTOM_CSS = """
<style>
[data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 0.5rem; }
[data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { min-width: 0 !important; }
[data-testid="stHorizontalBlock"] button { padding-left: 0.25rem; padding-right: 0.25rem; min-height: 40px; }
.block-container { max-width: 560px; padding-top: 2.5rem; }
h1 { font-size: 1.75rem !important; }
/* 체크박스 누르는 영역을 40px 이상으로 */
[data-testid="stCheckbox"] label { min-width: 40px; min-height: 40px; align-items: center; justify-content: center; margin: 0; }
.item-name { display: block; overflow-wrap: anywhere; line-height: 1.5; }
.item-name.done { color: rgba(128, 128, 128, 0.95); text-decoration: line-through; }
.empty-message { text-align: center; color: gray; padding: 2rem 0; margin: 0; }
.undo-message { overflow-wrap: anywhere; }
.page-info { text-align: center; color: gray; font-size: 0.875rem; }
.st-key-shop_js { display: none !important; }
</style>
"""

# 키보드 포커스 이동, 버튼 접근성 라벨, Esc로 수정 취소 — Streamlit이 직접 지원하지 않는 부분.
# __DATA__ 자리에 이번 실행의 데이터(JSON)가 들어간다.
ENHANCER_JS = """
<script>
(function () {
  const data = __DATA__;
  const doc = document;
  const S = window.__shoppingList || (window.__shoppingList = { kb: false, names: {}, installed: false });
  S.names = data.names;

  const LABELS = {
    check: ['input[type=checkbox]', ' 구매 완료'],
    editbtn: ['button', ' 수정'],
    delbtn: ['button', ' 삭제'],
    edit: ['input', ' 이름 수정'],
    save: ['button', ' 수정 저장'],
    cancel: ['button', ' 수정 취소'],
  };
  const KEY_RE = /^st-key-(check|editbtn|delbtn|edit|save|cancel)_([0-9a-f]{12})(?:_|$)/;

  function parseKey(el) {
    for (const c of el.classList) {
      const m = KEY_RE.exec(c);
      if (m) return { kind: m[1], h: m[2] };
    }
    return null;
  }

  // 각 컨트롤에 아이템 이름이 들어간 aria-label을 단다 (예: "우유 삭제")
  function applyLabels() {
    doc.querySelectorAll('[class*="st-key-"]').forEach((el) => {
      const k = parseKey(el);
      if (!k) return;
      const name = S.names[k.h];
      if (name == null) return;
      const [selector, suffix] = LABELS[k.kind];
      const target = el.querySelector(selector);
      const want = name + suffix;
      if (target && target.getAttribute('aria-label') !== want) target.setAttribute('aria-label', want);
    });
  }

  let scheduled = false;
  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; applyLabels(); restoreLostFocus(); });
  }

  // 되돌리기 알림이 사라지면서 포커스가 없어졌다면 입력창으로 보낸다
  function restoreLostFocus() {
    if (doc.activeElement !== doc.body || !S.lastFocus) return;
    if (!doc.contains(S.lastFocus) && S.lastFocusInUndo) {
      const input = doc.querySelector('.st-key-new_item input');
      if (input) input.focus({ preventScroll: true });
    }
    S.lastFocus = null;
  }

  if (!S.installed) {
    S.installed = true;
    // 마지막 입력이 키보드였는지 기억한다 (마우스·터치일 때는 포커스를 옮기지 않는다)
    doc.addEventListener('keydown', (e) => {
      S.kb = true;
      if (e.key !== 'Escape' || e.isComposing) return;
      const box = e.target.closest && e.target.closest('[class*="st-key-edit_"]');
      const k = box && parseKey(box);
      if (k && k.kind === 'edit') {
        const cancel = doc.querySelector('.st-key-cancel_' + k.h + ' button');
        if (cancel) { e.preventDefault(); e.stopPropagation(); cancel.click(); }
      }
    }, true);
    doc.addEventListener('pointerdown', () => { S.kb = false; }, true);
    doc.addEventListener('focusin', (e) => {
      S.lastFocus = e.target;
      S.lastFocusInUndo = !!e.target.closest('.st-key-undo_bar');
    });
    new MutationObserver(scheduleApply).observe(doc.body, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['aria-label'],
    });
  }
  scheduleApply();

  const f = data.focus;
  if (!f || (f.keyboardOnly && !S.kb)) return;
  const selector = {
    undo: '.st-key-undo_btn button',
    check: '[class*="st-key-check_' + f.h + '_"] input',
    editbtn: '.st-key-editbtn_' + f.h + ' button',
    edit: '.st-key-edit_' + f.h + ' input',
    added: '.st-key-row_' + f.h,
  }[f.kind];
  let tries = 0;
  (function attempt() {
    const el = selector && doc.querySelector(selector);
    if (!el) { if (++tries < 60) setTimeout(attempt, 50); return; }
    if (f.kind === 'added') {
      el.scrollIntoView({ block: 'nearest' });
      const input = doc.querySelector('.st-key-new_item input');
      if (input) input.focus({ preventScroll: true });
    } else {
      el.focus();
      if (f.kind === 'edit' && el.select) el.select();
    }
  })();
})();
</script>
"""


def to_script_json(value) -> str:
    """<script> 안에 넣어도 안전한 JSON (</script> 등으로 빠져나가지 못하게 이스케이프)."""
    return (
        json.dumps(value, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def text_html(text: str, css_class: str) -> str:
    """사용자 입력을 마크다운을 거치지 않고 HTML 이스케이프한 글자로만 표시한다."""
    return f'<span class="{css_class}">{html.escape(text)}</span>'


def render_item(item: dict) -> None:
    item_id = item["id"]
    h = widget_key(item_id)

    if item_id == st.session_state.editing_id:
        # 폼 안에 두면 입력창에서 Enter를 눌러도 저장된다
        with st.form(key=f"edit_form_{h}", border=True):
            col_input, col_save, col_cancel = st.columns([6, 1.3, 1.3], vertical_alignment="center")
            col_input.text_input(
                "이름 수정",  # 실제 라벨(아이템 이름 포함)은 JavaScript가 aria-label로 단다
                value=item["name"],
                max_chars=MAX_NAME_LENGTH,
                key=f"edit_{h}",
                label_visibility="collapsed",
            )
            col_save.form_submit_button("저장", type="primary", key=f"save_{h}",
                                        on_click=save_edit, args=(item_id,), width="stretch")
            col_cancel.form_submit_button("취소", key=f"cancel_{h}",
                                          on_click=cancel_edit, args=(item_id,), width="stretch")
        return

    with st.container(key=f"row_{h}"):
        col_check, col_name, col_edit, col_delete = st.columns([0.8, 6, 1.3, 1.3], vertical_alignment="center")
        # key에 체크 상태를 넣어, 다른 창에서 바뀐 값도 새 위젯으로 반영되게 한다
        check_key = f"check_{h}_{int(item['checked'])}"
        col_check.checkbox(
            "구매 완료",  # 라벨은 마크다운으로 해석되므로 사용자 입력을 넣지 않는다
            value=item["checked"],
            key=check_key,
            on_change=toggle_item,
            args=(item_id, check_key),
            label_visibility="collapsed",
        )
        with col_name:
            st.html(text_html(item["name"], "item-name done" if item["checked"] else "item-name"))
        col_edit.button("수정", key=f"editbtn_{h}", on_click=start_edit, args=(item_id,), width="stretch")
        col_delete.button("삭제", key=f"delbtn_{h}", on_click=delete_item, args=(item_id,), width="stretch")


def undo_bar() -> None:
    """삭제 직후 되돌리기 안내. 1초마다 이 부분만 다시 그려서 5초가 지나면 사라진다."""
    pending = st.session_state.last_deleted
    if not pending:
        return
    if time.time() - pending["at"] > UNDO_TIMEOUT_SEC:
        st.session_state.last_deleted = None
        return
    with st.container(border=True, key="undo_bar"):
        col_msg, col_btn = st.columns([4, 1.4], vertical_alignment="center")
        with col_msg:
            st.html(text_html(pending["message"], "undo-message"))
        if col_btn.button("되돌리기", type="primary", key="undo_btn", width="stretch"):
            undo_delete()
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒", layout="centered")
    st.html(CUSTOM_CSS)

    for key, default in [("editing_id", None), ("last_deleted", None), ("save_error", False),
                         ("corrupt_backup", None), ("page", 0), ("run", 0)]:
        st.session_state.setdefault(key, default)
    st.session_state.run += 1

    # 다른 창에서 바뀐 내용도 보이도록 매번 파일에서 다시 읽는다
    items = load_items()
    st.session_state.shopping_items = items
    if st.session_state.editing_id and find_index(items, st.session_state.editing_id) == -1:
        st.session_state.editing_id = None

    st.title("🛒 쇼핑 리스트", anchor=False)

    with st.form("add_form", clear_on_submit=True, border=False):
        col_input, col_add = st.columns([5, 1], vertical_alignment="bottom")
        col_input.text_input(
            "살 물건",
            placeholder="살 물건을 입력하세요",
            max_chars=MAX_NAME_LENGTH,
            key="new_item",
            label_visibility="collapsed",
        )
        col_add.form_submit_button("추가", type="primary", on_click=add_item, width="stretch")

    # 되돌리기 알림은 입력창 바로 아래에 둔다 (긴 목록에서도 보이고, Tab으로 금방 닿는다)
    if st.session_state.last_deleted:
        st.fragment(run_every=1)(undo_bar)()

    if st.session_state.corrupt_backup:
        st.warning(f"데이터 파일을 읽을 수 없어 {st.session_state.corrupt_backup} 로 백업하고 빈 목록으로 시작했습니다.")

    page_count = max(1, math.ceil(len(items) / PAGE_SIZE))
    st.session_state.page = min(max(st.session_state.page, 0), page_count - 1)
    start = st.session_state.page * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]

    if not items:
        st.html('<p class="empty-message">아직 추가한 물건이 없어요</p>')
    for item in page_items:
        render_item(item)

    if page_count > 1:
        col_prev, col_info, col_next = st.columns([1, 1, 1], vertical_alignment="center")
        col_prev.button("◀ 이전", key="page_prev", on_click=go_page, args=(-1,),
                        disabled=st.session_state.page == 0, width="stretch")
        col_info.html(f'<p class="page-info">{st.session_state.page + 1} / {page_count} 페이지</p>')
        col_next.button("다음 ▶", key="page_next", on_click=go_page, args=(1,),
                        disabled=st.session_state.page >= page_count - 1, width="stretch")

    st.divider()
    total = len(items)
    remaining = sum(1 for item in items if not item["checked"])
    col_count, col_clear = st.columns([3, 2], vertical_alignment="center")
    col_count.caption(f"{remaining} / {total} 남음" if total else "")
    col_clear.button("체크한 항목 삭제", on_click=clear_checked, disabled=remaining == total, width="stretch")

    if st.session_state.save_error:
        st.warning("목록을 파일에 저장하지 못했습니다.")

    data = {
        "run": st.session_state.run,  # 매 실행마다 스크립트가 다시 돌도록
        "names": {widget_key(item["id"]): item["name"] for item in page_items},
        "focus": st.session_state.pop("focus", None),
    }
    with st.container(key="shop_js"):
        st.html(ENHANCER_JS.replace("__DATA__", to_script_json(data)), unsafe_allow_javascript=True)


main()
