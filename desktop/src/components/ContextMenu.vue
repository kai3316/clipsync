<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { contextMenuFocus, contextMenuState, closeContextMenu, moveContextFocus, runContextMenuItem } from "../lib/context-menu";

/**
 * The window's one right-click menu, drawn at the root.
 *
 * It is a sibling of everything rather than a child of the row that opened it:
 * the menu is `position: fixed` and must not be clipped by the history list's
 * own scroll container, and the rows that open it are re-rendered by a poll
 * every few seconds — a menu owned by a row would be torn down mid-click when
 * its row was replaced.
 *
 * The legacy menu's four closing gestures are all here — a click outside,
 * Escape, a resize, and a scroll — plus blur, because a native window can lose
 * focus to another application and a menu left painted over a window nobody is
 * looking at is the kind of thing that stays on screen for an hour.
 */

const menu = contextMenuState();
const focused = contextMenuFocus();
const root = ref<HTMLElement | null>(null);

/** The measured height, so the bottom clamp uses the real footprint rather
 * than an estimate that leaves the last entry off-screen.  The legacy menu
 * carried a per-mode fallback for the same reason; here the first frame may
 * still use the estimate, and the measurement lands before the reader can
 * reach for anything. */
const height = ref(0);
const WIDTH = 224;
const EDGE = 8;

const style = computed(() => {
  const h = height.value || menu.items.length * 34 + 16;
  let { x, y } = menu;
  const maxX = window.innerWidth - WIDTH - EDGE;
  const maxY = window.innerHeight - h - EDGE;
  if (x > maxX) x = maxX;
  if (y > maxY) y = maxY;
  if (x < EDGE) x = EDGE;
  if (y < EDGE) y = EDGE;
  return { left: `${x}px`, top: `${y}px`, width: `${WIDTH}px` };
});

/** Measure after every open, and after the entries change under an open menu. */
watch(() => [menu.open, menu.items] as const, async () => {
  if (!menu.open) { height.value = 0; return; }
  await nextTick();
  height.value = root.value?.offsetHeight || 0;
  root.value?.focus();
}, { immediate: true });

function hover(index: number) {
  focused.value = index;
}
function onPointerDown(event: MouseEvent) {
  if (!menu.open) return;
  if (root.value?.contains(event.target as Node)) return;
  closeContextMenu();
}
function onKeydown(event: KeyboardEvent) {
  if (!menu.open) return;
  switch (event.key) {
    case "Escape": event.preventDefault(); closeContextMenu(); return;
    case "ArrowDown": event.preventDefault(); moveContextFocus(1); return;
    case "ArrowUp": event.preventDefault(); moveContextFocus(-1); return;
    case "Home": event.preventDefault(); focused.value = 0; return;
    case "End": event.preventDefault(); focused.value = menu.items.length - 1; return;
    case "Enter":
    case " ": {
      const item = menu.items[focused.value];
      if (!item) return;
      event.preventDefault();
      void runContextMenuItem(item);
      return;
    }
  }
}
// `capture`, because the rows this menu is opened from handle their own clicks
// and a plain listener would let one of them swallow the dismissal.
onMounted(() => {
  window.addEventListener("pointerdown", onPointerDown, true);
  window.addEventListener("keydown", onKeydown, true);
  window.addEventListener("resize", closeContextMenu);
  window.addEventListener("blur", closeContextMenu);
  window.addEventListener("scroll", closeContextMenu, true);
});
onUnmounted(() => {
  window.removeEventListener("pointerdown", onPointerDown, true);
  window.removeEventListener("keydown", onKeydown, true);
  window.removeEventListener("resize", closeContextMenu);
  window.removeEventListener("blur", closeContextMenu);
  window.removeEventListener("scroll", closeContextMenu, true);
});
</script>

<template>
  <div
    v-if="menu.open"
    ref="root"
    class="context-menu"
    role="menu"
    tabindex="-1"
    :style="style"
    @contextmenu.prevent
    @pointerdown.stop
  >
    <template v-for="(item, index) in menu.items" :key="item.id">
      <hr v-if="item.divider && index" class="context-menu-rule" />
      <button
        class="context-menu-item"
        :class="{ 'is-danger': item.danger, 'is-focused': focused === index }"
        role="menuitem"
        type="button"
        :aria-disabled="item.disabled || undefined"
        :title="item.title || undefined"
        @mouseenter="hover(index)"
        @click="runContextMenuItem(item)"
      >
        <component :is="item.icon" v-if="item.icon" :size="15" />
        <span class="context-menu-label">{{ item.label }}</span>
        <span v-if="item.shortcut" class="context-menu-shortcut">{{ item.shortcut }}</span>
      </button>
    </template>
  </div>
</template>

<style scoped>
.context-menu {
  position: fixed;
  /* Below the modal layer: a menu must never float over a confirm dialog it
     just opened — the legacy menu sat at 390 for the same reason. */
  z-index: 390;
  padding: 5px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: 0 16px 40px rgb(0 0 0 / 26%);
  outline: none;
}
.context-menu-rule { height: 1px; margin: 5px 4px; border: 0; background: var(--line); }
.context-menu-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 9px;
  border: 0;
  border-radius: calc(var(--radius) - 4px);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
/* Hover and the keyboard cursor are the same highlight, so the two ways of
   pointing at an entry read identically. */
.context-menu-item:hover,
.context-menu-item.is-focused { background: var(--accent-soft); color: var(--accent-text); }
.context-menu-item.is-danger { color: var(--danger-text); }
.context-menu-item.is-danger:hover,
.context-menu-item.is-danger.is-focused { background: var(--danger-soft, rgb(200 40 40 / 14%)); }
.context-menu-item[aria-disabled] { opacity: .45; cursor: not-allowed; }
.context-menu-item[aria-disabled]:hover,
.context-menu-item[aria-disabled].is-focused { background: transparent; color: inherit; }
.context-menu-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.context-menu-shortcut { color: var(--muted); font-size: 11px; }
</style>
