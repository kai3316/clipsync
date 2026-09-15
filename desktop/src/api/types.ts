export interface AppStatus {
  version: string;
  health: "starting" | "locked" | "ready" | "stopped";
  device_name: string;
  device_id: string;
  language: string;
  sync_state: string;
  runtime_error?: string;
  capabilities: string[];
  session_id: string;
  seq: number;
}

export interface Settings {
  settings: Record<string, unknown>;
}

export interface BackupItem {
  path: string;
  filename?: string;
  date?: string;
  size?: number;
}

export interface Transfer {
  id: string;
  filename: string;
  size: number;
  direction: string;
  status: string;
  progress?: number;
  speed?: number;
  eta?: string;
  reason?: string;
  path?: string;
  peer_id?: string;
  timestamp?: number;
}

export interface TransfersPage {
  active: Transfer[];
  history: Transfer[];
  speed_test?: Record<string, unknown>;
}

/**
 * What a history row's hover card can add about it.
 *
 * The card over a text row repeats the preview the row had to clamp.  An image
 * and a file row have no words to repeat, so their card is this instead: a
 * picture for the image, a list for the files, and `kind: ""` for every row
 * there is nothing to add about — which is answered rather than raised, because
 * a pointer crossing a row is not a request anybody made.
 */
export interface HistoryPreview {
  /** `"image"`, `"files"`, or `""` for a row with nothing more to show. */
  kind: string;
  /** A `data:` URL, already downscaled to a card's width.  `""` when there is none. */
  image: string;
  /** The *source* picture's size, so a card can caption a 4000 px screenshot as one. */
  width: number;
  height: number;
  files: HistoryPreviewFile[];
  /** How many files the clip holds, which can exceed the ones listed here. */
  total: number;
}

export interface HistoryPreviewFile {
  name: string;
  /** In bytes; `0` for a folder. */
  size: number;
  kind: "file" | "dir";
  /**
   * Whether the file is still on this machine.  Always true for a file that
   * lives on another device: it never was here, and the card must not read
   * that as a file that has gone missing.
   */
  exists: boolean;
}

export interface HistoryItem {
  id: string;
  timestamp: number;
  preview: string;
  content_type: string;
  pinned: boolean;
  /** Name of the device the clip synced from — this one, a peer, or the phone. */
  source_name: string;
  /**
   * The device id that name stands for, which is what a download request has to
   * name.  Empty for a clip captured on this machine.  Optional because the
   * sidecar only started sending it alongside `FILE_REMOTE` rows.
   */
  source_device?: string;
  /**
   * Which link a remote clip arrived on: `"lan"` for a peer on a direct
   * connection, `"relay"` for one that came through the internet relay, and
   * `""` for a clip captured here or a row written before the route was
   * recorded.  `source_name` cannot answer this — a peer paired both ways sends
   * over whichever is up.
   *
   * Optional because the sidecar only started sending it with the route: a
   * window running against an older one gets no key at all, and a row with no
   * route shows no chip rather than a guess.
   */
  transport?: string;
  /** The application it was copied in, when source tracking is on. */
  source_app: string;
  /** That application's window title, when source tracking is on. */
  source_title: string;
  /** How many times the clip has been pasted back. */
  paste_count: number;
}

export interface HistoryPage {
  session_id: string;
  seq: number;
  total: number;
  offset: number;
  items: HistoryItem[];
  /** How many rows each kind chip would show under the current search. */
  counts?: Record<string, number>;
  /** Whether any history exists at all, ignoring the search and the filter. */
  has_history?: boolean;
}

export interface Device {
  id: string;
  name: string;
  note?: string;
  paired: boolean;
  connection_state: string;
  pairing_status: string;
  pairing_code: string | null;
  sas: string | null;
  archived?: boolean;
  removed_at?: number;
  /** Set while the transport is working through its reconnect attempts at an
   *  offline paired peer, with the count the row's chip reports.  Absent on a
   *  connected row and on one the transport is not retrying. */
  reconnecting?: boolean;
  reconnect_attempt?: number;
  reconnect_max?: number;
  /** What the peer advertises about itself in its mDNS records.  All three are
   *  empty for a device this build has never seen announce them — a peer older
   *  than those fields, or a row with no live sighting — so an empty version
   *  means unknown, never "up to date". */
  version?: string;
  platform?: string;
  arch?: string;
  /** Whether this build could update that device — same platform, and a
   *  version this one is ahead of.  The sidecar decides it: the comparison and
   *  the platform spelling are both its, and a second implementation of either
   *  would be a second answer to the same question. */
  update_available?: boolean;
  /** Whether an offer would carry the installer or only the news. */
  update_cached?: boolean;
}

export interface DeviceProbeChannel {
  channel: "lan" | "relay";
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
}

export interface DeviceProbeResult {
  ok: boolean;
  results: DeviceProbeChannel[];
  error?: string;
}

export interface DeviceCertificate {
  device_id: string;
  device_name: string;
  fingerprint_short: string;
  fingerprint: string;
  paired: boolean;
}

export interface InternetPairingPeer {
  peer_id: string;
  name: string;
  alias: string;
  online: boolean;
  last_seen?: number;
  paired: boolean;
}

export interface InternetPairingStatus {
  generated_code?: string | null;
  peers: InternetPairingPeer[];
  /** Whether internet sync is on, which is what decides whether the relay is
   *  up at all.  Optional because a sidecar older than the switch sends no
   *  key, and the page reads that as "off" rather than as "unknown". */
  enabled?: boolean;
  /**
   * Codes entered on this machine that the other side has not answered yet.
   *
   * The other direction from `generated_code`: that is a code *we* made and are
   * waiting for somebody to type, this is a code somebody made and we typed.
   * Distinct from `peers` because it is not a device — the entry is keyed by the
   * 4-char tag the code carried, which cannot be reached, sent to, or renamed
   * until the partner's reply supplies their real device id.  It is what the
   * page shows between submitting a code and the pairing completing, which is
   * the only thing the reader has to go on in that window.
   *
   * Optional: a sidecar that predates it sends no key at all.
   */
  waiting?: InternetPairingWait[];
}

export interface InternetPairingWait {
  peer_id: string;
  /** Learned from the partner's hello, empty until it arrives. */
  name?: string;
  /** When the code was entered, as a unix timestamp; null after a restart,
   * which loses the clock but not the wait. */
  since?: number | null;
}

/**
 * What a relay broker test found, one row per broker tried.
 *
 * The rows come back sorted by the sidecar — reachable first, then by ascending
 * latency — which is the order the old panel rendered and the reason this type
 * carries no ordering of its own for the view to re-apply.
 *
 * `summary` is deliberately absent: the sidecar returns the two counts and each
 * front words its own sentence, because the old panel's English "2/3 reachable"
 * is not a sentence this window can show in Chinese.
 */
export interface RelayTestResult {
  results: RelayProbeRow[];
  reachable: number;
  total: number;
}

export interface RelayProbeRow {
  endpoint: string;
  ok: boolean;
  /** Milliseconds to a completed handshake; null where the probe never got one. */
  latency_ms?: number | null;
  /** Why it failed, in the probe's own words — shown as received. */
  detail?: string;
}

export interface SidecarEvent {
  type: "event" | "resync";
  name?: string;
  session_id: string;
  seq?: number;
  data?: Record<string, unknown>;
}

export interface BridgeError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface FavoriteSummary {
  id: string;
  title: string;
  preview: string;
  group: string;
  position: number;
  created: number;
  updated: number;
}

export interface Favorite extends Omit<FavoriteSummary, "preview"> {
  content: string;
}

export interface FavoritesPage {
  items: FavoriteSummary[];
  /** How many favourites match the search and group filter, not the page. */
  total: number;
  offset: number;
  groups: string[];
  /** Every group's size, counted over the whole library rather than the
   * search, so the sidebar does not renumber itself while the reader types.
   * A group with nothing in it yet is present and counts zero. */
  group_counts?: Record<string, number>;
  /** The whole library's size, for the "all groups" row. */
  library_total?: number;
  session_id: string;
  seq: number;
}

export interface ChatSession {
  session_id: string;
  peer_id: string;
  peer_name: string;
  fingerprint_short: string;
  status: string;
  created_ts: number;
  last_activity_ts: number;
  unread: number;
  online: boolean;
  last_preview: string;
  peer_typing?: boolean;
}

export interface ChatEntry {
  entry_id: string;
  kind: string;
  outgoing: boolean;
  ts: number;
  text: string;
  text_key: string;
  fmt: Record<string, unknown>;
  file_name: string;
  file_size: number;
  mime: string;
  status: string;
  fraction: number;
  saved_path: string;
  transfer_id: string;
  msg_id: string;
}

export interface ChatSessionsPage {
  sessions: ChatSession[];
  muted?: string[];
  // Whether nearby devices may send without asking.  An entry sits at
  // `await_accept` for an instant in both modes, so this is what says whether
  // the Accept button under it is one the user is actually expected to press.
  open_to_all?: boolean;
}

export interface ChatMessagesPage {
  messages: ChatEntry[];
}

export type DiagnosticStatus = "ok" | "warn" | "fail";

// The sidecar resolves the report's i18n keys for this client (they live in the
// web panel's catalog, not the shell's), attaching `*_text` fields; the
// `*_key`/`*_params` pairs stay so the web panel keeps resolving them itself.
export interface DiagnosticCheck {
  id: string;
  ok: boolean;
  detail: string;
  guidance?: string | null;
  detail_key?: string;
  detail_params?: Record<string, unknown>;
  guidance_key?: string | null;
  guidance_params?: Record<string, unknown>;
  label_text?: string;
  detail_text?: string;
  guidance_text?: string;
}

export interface DiagnosticItem {
  id: string;
  status: DiagnosticStatus;
  detail: string;
  hint?: string | null;
  detail_key?: string;
  hint_key?: string;
  label_text?: string;
  detail_text?: string;
  hint_text?: string;
}

export interface DiagnosticGroup {
  label_key: string;
  label_text?: string;
  items: DiagnosticItem[];
}

export interface DiagnosticsReport {
  v2: boolean;
  summary: DiagnosticStatus;
  checks: DiagnosticCheck[];
  groups: Record<string, DiagnosticGroup>;
  discovery_running: boolean;
  server_running: boolean;
  connected_count: number;
  paired_count: number;
  web_companion_running: boolean;
  web_port: number;
  lan_ip: string;
  os: string;
  version: string;
}

export type DiagnosticAction = "firewall" | "local_network";

// The update lifecycle is owned by the sidecar; this client only mirrors it.
// `fraction` is 0–1 and `downloaded`/`total` are bytes (0 when unknown).
// `installing` is the one phase the sidecar never emits: replacing the running
// installation is the host's job, and it publishes that phase itself.
export type UpdatePhase = "idle" | "downloading" | "ready" | "installing" | "failed";

export interface UpdateState {
  phase: UpdatePhase;
  fraction: number;
  downloaded: number;
  total: number;
  error: string;
  version: string;
  path: string;
}

export interface UpdateStatusResult {
  state: UpdateState;
}

export interface UpdateCheckResult {
  available: boolean;
  latest: string;
  current: string;
  url: string;
  /** Whether this build can install what it finds in place, which only the
   *  host knows: it is the updater plugin matching this machine's bundle
   *  against the release manifest.  Optional because it is the host's addition
   *  and an older one does not send it. */
  installable?: boolean;
  /** Why the check has no answer, when it has none: the network error, the
   *  HTTP status and GitHub's own message for it, or a reply with no release
   *  in it.  Empty when `latest` is a real answer.  Optional for the same
   *  reason as `installable` — an older host does not send it, and the panel
   *  falls back to the sentence it used before. */
  error?: string;
}

export interface UpdateInstallResult {
  ok: boolean;
  /** False only when there was nothing to install — a reply at all means the
   *  install did not happen, because a successful one replaces this process. */
  installed: boolean;
  /** `up_to_date` when the manifest had nothing newer than what is running. */
  reason?: string | null;
}

export interface UpdateDownloadResult {
  ok: boolean;
  started: boolean;
  error?: string | null;
}

export interface UpdateOpenFolderResult {
  ok: boolean;
  error?: string;
}

/** One artifact a repair moved aside: `artifact` is `config` or `history`, and
 * `reason` is a stable code (`unreadable`, `identity`, `history_corrupt`,
 * `history_identityless`, `history_identity_lost`) the UI turns into text. */
export interface RecoveryItem {
  artifact: string;
  reason: string;
  /** The archived names, `config.json.corrupt-<stamp>` style. */
  files: string[];
}

export interface RecoveryResult {
  /** Empty when the data directory was already usable. */
  items: RecoveryItem[];
}

/** One clip in the overview's activity feed.
 *
 * The same row the history page lists, only with a shorter preview — the feed
 * is the newest few of that list.  It used to be a shape of its own (`text` /
 * `type` / `time`), which named nothing and so could do nothing: the row could
 * only jump to the page that owns the real ones. */
export type OverviewRecentItem = HistoryItem;

/** The dashboard counters the phone Companion and the legacy web dashboard
 * both render, now read by the window's own overview page. */
export interface Overview {
  connected_count: number;
  paired_count: number;
  discovered_count: number;
  connected_names: string[];
  history_count: number;
  history_today: number;
  history_pinned: number;
  history_images: number;
  active_transfers: number;
  transfer_completed: number;
  discovering: boolean;
  visible: boolean;
  sync_enabled: boolean;
  web_enabled: boolean;
  uptime_seconds: number;
  local_ip: string;
  port: number;
  platform: string;
  version: string;
  network_type: string;
  network_detail: string;
  recent_items: OverviewRecentItem[];
}
