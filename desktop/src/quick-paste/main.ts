import { createApp } from "vue";
import QuickPaste from "./QuickPaste.vue";
// The overlay is a second window rather than a second page of the shell, so it
// pays for its own stylesheet — but the tokens, the dark theme and the base
// controls (the search box, the chips) are the shell's, imported rather than
// copied: a quick-paste row that was a different green from every other green
// in the application would be a second design.
import "../styles.css";
import "./quick-paste.css";

createApp(QuickPaste).mount("#app");
