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

export interface HistoryItem {
  id: string;
  timestamp: number;
  preview: string;
  content_type: string;
  pinned: boolean;
  /** Name of the device the clip synced from — this one, a peer, or the phone. */
  source_name: string;
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
  total: number;
  offset: number;
  groups: string[];
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
export type UpdatePhase = "idle" | "downloading" | "ready" | "failed";

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
