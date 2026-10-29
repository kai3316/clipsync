import { createApp } from "vue";
import App from "./App.vue";
import "./styles.css";

createApp(App).mount("#app");

// The static first frame in `index.html` stays until the window knows what the
// sidecar is doing.  Removing it on mount was a loaded-but-empty window: the
// sidecar had not answered yet, so the shell had nothing to draw and the boot
// skeleton had already been taken away.  `App.vue` removes it when a status or
// a startup error arrives.
