<script setup lang="ts">
/**
 * A list with nothing in it, and the one thing to do about it.
 *
 * Three pages said 暂无 in their own way and stopped there — the chat, the
 * favourites and the transfers — while the devices page was the only empty
 * state in the window that told the reader what would fill it.  A sentence with
 * no next step leaves a first run guessing at how the page works, and the guess
 * usually costs a trip to the settings page or nothing at all.
 *
 * `size` is the only styling decision: `page` is a page body with nothing on it
 * (taller, larger icon), `inset` is the same state inside a card or a sidebar
 * column, where a 310px placeholder would be a hole in a list of rows.
 */
import type { Component } from "vue";

withDefaults(defineProps<{
  icon?: Component;
  title: string;
  description?: string;
  /** The main action's label; an empty string draws no button. */
  action?: string;
  secondary?: string;
  disabled?: boolean;
  size?: "page" | "inset";
}>(), { size: "page" });

const emit = defineEmits<{ action: []; secondary: [] }>();
</script>

<template>
  <div class="empty" :class="size === 'page' ? 'empty--page' : undefined">
    <component :is="icon" v-if="icon" :size="size === 'page' ? 36 : 30" />
    <h2>{{ title }}</h2>
    <p v-if="description" class="note">{{ description }}</p>
    <div v-if="action || secondary" class="empty-actions">
      <button v-if="action" class="primary" :disabled="disabled" @click="emit('action')">{{ action }}</button>
      <button v-if="secondary" :disabled="disabled" @click="emit('secondary')">{{ secondary }}</button>
    </div>
  </div>
</template>
