export type PlatformStatus = "ok" | "not_found" | "error" | "skipped" | "missing";

export interface ApiEnvelope<T> {
  ok: boolean;
  data: T;
  error?: { code: string; message: string };
}

export interface TitleCase {
  case_id: string;
  file_id: string;
  current_name: string;
  current_body: string;
  extension: string;
  canonical_path: string;
  core_title: string;
  readable_title: string;
  query_title: string;
  author: string | null;
  effective_max: number;
  unit: string;
  complete: boolean;
  assignment_state: string;
  protected: boolean;
  representative: boolean;
  platforms: Record<"series" | "kakao" | "novelpia", PlatformStatus>;
  source_revision: string;
  editable: boolean;
  blocked_reasons: string[];
}

export interface TitleListing {
  items: TitleCase[];
  total: number;
  limit: number;
  cursor: string | null;
  next_cursor: string | null;
  sort: string;
  direction: string;
  status_filter: string;
  search: string;
}

export interface TitlePreview {
  file_id: string;
  source_revision: string;
  source_path: string;
  current_name: string;
  current_body: string;
  before_core_title: string;
  new_body: string;
  candidate_name: string;
  materialized_candidate_name: string;
  destination_path: string;
  after_core_title: string;
  after_readable_title: string;
  after_query_title: string;
  after_author: string | null;
  after_effective_max: number;
  after_unit: string;
  after_volume_coordinate: string | null;
  after_complete: boolean;
  title_literal_tokens: string[];
  structure_hint_tokens: string[];
  target_exists: boolean;
  target_has_ok: boolean;
  blocked_reasons: string[];
  runnable: boolean;
}

export interface TitlePlan {
  version: string;
  provider: string;
  item_count: number;
  blocked_count: number;
  plan_sha256: string;
  runnable: boolean;
  items: TitlePreview[];
}

export type VolumeClassification = "auto_ready" | "review_required" | "already_grouped" | "excluded";

export interface VolumeItem {
  file_id: string;
  name: string;
  canonical_path: string;
  parent: string;
  extension: string;
  size: number;
  author: string | null;
  coordinate_kind: string;
  coordinate: string;
  coordinate_raw: string | null;
  effective_max: number;
  unit: string;
  complete: boolean;
  span_ambiguous: boolean;
  same_coordinate_count: number;
  issues: string[];
  assignment_state: string;
  assignment_origin: string | null;
  variant_id: number | null;
  work_bucket_id: number | null;
  protected: boolean;
  representative: boolean;
}

export interface VolumeCase {
  provider: "volume_group";
  case_id: string;
  source_revision: string;
  core_title: string;
  display_title: string;
  classification: VolumeClassification;
  file_count: number;
  parent_count: number;
  parents: string[];
  coordinate_kinds: string[];
  coordinate_range: [string, string];
  duplicate_coordinates: string[];
  approved_duplicate_coordinates: string[];
  unapproved_duplicate_coordinates: string[];
  parallel_format_coordinates: string[];
  missing_coordinates: string[];
  authors: string[];
  work_bucket_ids: number[];
  target_folder_name: string;
  target_folder_path: string;
  blocked_reasons: string[];
  plan_ready: boolean;
  items: VolumeItem[];
}

export interface VolumeListing {
  items: VolumeCase[];
  total: number;
  summary: Record<VolumeClassification, number>;
  limit: number;
  cursor: string | null;
  next_cursor: string | null;
  search: string;
  classification: string;
  sort: string;
  direction: string;
  readonly: boolean;
}

export interface VolumePreview {
  provider: "volume_group";
  case_id: string;
  source_revision: string;
  selected_file_ids: string[];
  target_folder_name: string;
  allow_duplicate_coordinates: boolean;
  destination_root: string;
  tree: string[];
  moved_count: number;
  preserved_source_items: string[];
  blocked_reasons: string[];
  item_count: number;
  plan_sha256: string;
  plan_ready: boolean;
  apply_available: boolean;
  readonly_reason: string | null;
  items: VolumeItem[];
}

export interface JobRecord {
  job_id: string;
  job_type: string;
  state: string;
  stage: string;
  message: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  progress: { current: number; total: number };
  result: Record<string, unknown> | null;
  error: { code: string; message: string } | null;
  payload?: Record<string, unknown>;
  last_event?: JobEvent | null;
}

export interface JobEvent {
  recorded_at: string;
  phase: string;
  [key: string]: unknown;
}

export interface DedupReportItem {
  report_id: string;
  name: string;
  kind: "dedup" | "strong_candidates";
  size: number;
  created_at: string;
  modified_at: string;
  summary: string;
  text_available: boolean;
  structured_available: boolean;
}

export interface DedupReportListing {
  items: DedupReportItem[];
  total: number;
  limit: number;
  cursor: string | null;
  next_cursor: string | null;
  search: string;
  kind: "all" | "dedup" | "strong_candidates";
  readonly: true;
  root: string;
}

export interface DedupReportDetail extends DedupReportItem {
  text: string;
  structured_summary: Record<string, unknown> | null;
  structured_metadata: Record<string, unknown> | null;
  readonly: true;
}

export interface ServiceDescriptor {
  id: string;
  job_type: string;
  label: string;
  summary: string;
  category: string;
  quick_action: boolean;
  target_label: string;
  target_count: number;
  read_scope: string[];
  write_scope: string[];
  defaults: string[];
  ready: boolean;
  blocked_code: string | null;
  blocked_reason: string | null;
  configured: boolean;
  doctor_ok: boolean;
  preview: Record<string, unknown>;
  active_job: JobRecord | null;
  latest_job: JobRecord | null;
}

export interface DashboardData {
  version: string;
  database: {
    integrity: string;
    doctor_scope: "operational" | "full";
    doctor_ok: boolean;
    doctor_issue_count: number;
    supported_house_files: number;
    catalog_titles: number;
    titles_without_ok_metadata: number;
    pending_reviews: number;
  };
  filesystem: {
    folderling_pending: number;
    warning_files: number;
    index: {
      exists: boolean;
      files: number;
      directories: number;
      normalizer_version?: string;
    };
  };
  next_actions: Array<{
    code: string;
    label: string;
    detail: string;
    href: string;
    severity: "action" | "warning" | "error" | "info";
  }>;
  jobs: JobRecord[];
}

export interface CatalogPlatform {
  platform: "series" | "kakao" | "novelpia";
  status: PlatformStatus;
  remote_title?: string | null;
  remote_url?: string | null;
  download_count?: number | null;
  view_count?: number | null;
  recommend_count?: number | null;
  rating?: number | null;
  rating_count?: number | null;
  last_attempt_at?: string | null;
  last_success_at?: string | null;
  retry_after?: string | null;
  error_message?: string | null;
}

export interface CatalogFile {
  file_id: string;
  name: string;
  path: string;
  readable_title: string;
  author: string | null;
  effective_max: number;
  unit: string;
  complete: boolean;
}

export interface CatalogItem {
  title_key: string;
  display_title: string;
  query_title: string;
  author: string | null;
  file_count: number;
  effective_max: number;
  unit: string;
  complete: boolean;
  files: CatalogFile[];
  work_bucket_ids: number[];
  variant_ids: number[];
  folders: string[];
  representative_file_ids: string[];
  platforms: Record<"series" | "kakao" | "novelpia", CatalogPlatform>;
}

export interface CatalogListing {
  items: CatalogItem[];
  total: number;
  limit: number;
  cursor: string | null;
  next_cursor: string | null;
  search: string;
  status: string;
  readonly: true;
}

export interface WorkManagementDetail {
  work: {
    work_bucket_id: number;
    display_title: string | null;
    status: "active" | "retired";
  };
  variants: Array<{
    variant_id: number;
    variant_kind: string;
    label: string | null;
    status: "active" | "retired";
    active_file_count: number;
    representative_file_id: string | null;
  }>;
  folders: Array<{
    folder_id: number;
    canonical_path: string;
    role: "primary" | "edition" | "auxiliary";
    state: string;
  }>;
  aliases: Array<{
    alias_id: number;
    alias_kind: "core_title" | "readable_title" | "folder_name";
    alias_key: string;
    alias_display: string;
    work_bucket_id: number;
    preferred_folder_id: number | null;
    origin: string;
    active: number;
  }>;
  files: Array<{
    file_id: string;
    canonical_path: string;
    variant_id: number;
    active: number;
    source: string;
    size: number;
    coordinate_kind: string | null;
    coordinate_raw: string | null;
    representative: number;
  }>;
  events: Array<Record<string, unknown>>;
  readonly: true;
}

export interface WorkMergePlan {
  version: "1.3.5";
  kind: "work_merge";
  item_count: number;
  source: WorkManagementDetail;
  target: WorkManagementDetail;
  demoted_folder_ids: number[];
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
}

export interface WorkSplitPlan {
  version: "1.3.5";
  kind: "work_split";
  item_count: number;
  source: WorkManagementDetail;
  variant_ids: number[];
  folder_ids: number[];
  alias_ids: number[];
  display_title: string;
  cleared_alias_routes: number[];
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
}

export interface WorkAliasPlan {
  version: "1.3.5";
  kind: "work_alias_upsert";
  item_count: number;
  alias_kind: "core_title" | "readable_title" | "folder_name";
  alias_key: string;
  alias_display: string;
  work: WorkManagementDetail["work"];
  preferred_folder: WorkManagementDetail["folders"][number] | null;
  existing_alias: WorkManagementDetail["aliases"][number] | null;
  replace_alias_id: number | null;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
}

export interface WorkAliasRetirePlan {
  version: "1.3.5";
  kind: "work_alias_retire";
  item_count: number;
  alias: WorkManagementDetail["aliases"][number];
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
}

export interface RepresentativePlan {
  version: "1.3.5";
  kind: "representative_replace";
  item_count: number;
  variant: WorkManagementDetail["variants"][number] & { work_bucket_id: number };
  file: WorkManagementDetail["files"][number] | null;
  current_file_id: string | null;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
}

export interface ExplorerFile {
  file_id: string;
  canonical_path: string;
  name: string;
  parent: string;
  extension: string;
  source: string;
  active: boolean;
  size: number;
  mtime_ns: number;
  last_seen_at: string;
  assignment_state: string;
  assignment_origin: string | null;
  variant_id: number | null;
  protected: boolean;
  representative: boolean;
  work_bucket_id: number | null;
  variant_kind: string | null;
  variant_label: string | null;
  core_title: string | null;
  readable_title: string | null;
  catalog_query_title: string | null;
  author: string | null;
  coordinate_kind: string | null;
  coordinate_raw: string | null;
  part_num: number | null;
  part_den: number | null;
  volume_num: number | null;
  volume_den: number | null;
  coordinate_symbol: string | null;
  episode_start: number | null;
  episode_end: number | null;
  effective_max: number;
  unit: string;
  complete: boolean;
  fingerprint_id: number | null;
  fingerprint_status: string | null;
  raw_sha256: string | null;
  normalized_sha256: string | null;
  normalized_length: number | null;
  open_review_count: number;
  retired_virtual_path: boolean;
}

export interface ExplorerFileListing {
  items: ExplorerFile[];
  total: number;
  limit: number;
  cursor: string | null;
  next_cursor: string | null;
  search: string;
  source: string;
  extension: string;
  sort: string;
  direction: string;
  readonly: true;
}

export interface ExplorerHistoryItem {
  review_id?: number;
  decision_id?: number;
  operation_id?: number;
  run_id?: string;
  classification?: string;
  verdict?: string;
  action?: string;
  state?: string;
  operation_state?: string;
  source_path?: string | null;
  dest_path?: string | null;
  quarantine_path?: string | null;
  created_at?: string;
  updated_at?: string;
  decided_at?: string;
  note?: string | null;
  evidence?: unknown;
}

export interface ExplorerFileDetail {
  file: ExplorerFile & Record<string, unknown>;
  reviews: ExplorerHistoryItem[];
  decisions: ExplorerHistoryItem[];
  operations: ExplorerHistoryItem[];
  same_coordinate: Array<{ file_id: string; canonical_path: string; size: number; source: string; active: number; author: string | null }>;
  actions: { compare: boolean; title_correction: boolean; quarantine: boolean; move: false; blocked_reasons: string[]; quarantine_blocked_reasons: string[]; future_version: string };
  readonly: true;
}

export interface FileRelocatePlan {
  version: "1.3.5";
  kind: "file_relocate";
  item_count: number;
  source: ExplorerFile & Record<string, unknown>;
  target_directory: string;
  target_name: string;
  destination_path: string;
  rename: boolean;
  move: boolean;
  projection_same: boolean;
  projection_diff: {
    analysis: Record<string, { before: unknown; after: unknown }>;
    coordinate: Record<string, { before: unknown; after: unknown }>;
  };
  route: "journaled_relocate" | "title_correction";
  title_correction_search: string;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  readonly: true;
}

export interface ManagedFolderPlan {
  version: "1.3.5";
  kind: "managed_folder_create";
  item_count: number;
  work: { work_bucket_id: number; display_title: string | null };
  parent_directory: string;
  folder_name: string;
  destination_path: string;
  role: "primary" | "edition" | "auxiliary";
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  readonly: true;
}

export interface ManagedFolderRelocatePlan {
  version: "1.3.5";
  kind: "managed_folder_relocate";
  item_count: number;
  folder: { folder_id: number; work_bucket_id: number; role: string; display_title: string | null };
  source_path: string;
  target_parent: string;
  target_name: string;
  destination_path: string;
  rename: boolean;
  move: boolean;
  registered_count: number;
  auxiliary_count: number;
  directory_count: number;
  total_size: number;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  readonly: true;
}

export interface ManagedFolderAdoptPlan {
  version: "1.3.5";
  kind: "managed_folder_adopt";
  item_count: number;
  folder_path: string;
  work: { work_bucket_id: number; display_title: string | null };
  role: "primary" | "edition" | "auxiliary";
  found_work_ids: number[];
  registered_count: number;
  auxiliary_count: number;
  directory_count: number;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  readonly: true;
}

export interface WorkSearchItem {
  work_bucket_id: number;
  display_title: string | null;
  active_file_count: number;
  active_folder_count: number;
}

export interface WorkSearchListing {
  items: WorkSearchItem[];
  search: string;
  limit: number;
  readonly: true;
}

export interface FileDestinationCandidate {
  path: string;
  name: string;
  relative_path: string;
  file_count: number;
  total_size: number;
  core_titles: string[];
  work_bucket_ids: number[];
  managed_folder_id: number | null;
  managed_role: string | null;
  similarity: number;
  score: number;
  reasons: string[];
  current: boolean;
}

export interface FileDestinationListing {
  source: {
    file_id: string;
    path: string;
    core_title: string;
    readable_title: string;
    work_bucket_id: number | null;
    current_parent: string;
  };
  items: FileDestinationCandidate[];
  search: string;
  limit: number;
  readonly: true;
}

export interface ExplorerComparison {
  left: ExplorerFile & Record<string, unknown>;
  right: ExplorerFile & Record<string, unknown>;
  comparison: {
    same_core_title: boolean;
    same_author: boolean;
    same_coordinate: boolean;
    same_raw_sha256: boolean;
    same_normalized_sha256: boolean;
    size_delta: number;
  };
  latest_review: ExplorerHistoryItem | null;
  latest_decision: ExplorerHistoryItem | null;
  latest_pair_cache: (ExplorerHistoryItem & { classification?: string }) | null;
  relationship_preview: { available_verdicts: string[]; apply_available: false; future_version: string };
  readonly: true;
}

export interface ExplorerFolder {
  path: string;
  name: string;
  relative_path: string;
  file_count: number;
  total_size: number;
  core_titles: string[];
  work_bucket_ids: number[];
  variant_ids: number[];
  sample_files: string[];
  mixed_core: boolean;
  mixed_work: boolean;
  depth: number;
  managed_folder_id: number | null;
  managed_role: "primary" | "edition" | "auxiliary" | null;
  managed_work_title: string | null;
}

export interface ExplorerFolderListing {
  items: ExplorerFolder[];
  total: number;
  limit: number;
  cursor: string | null;
  next_cursor: string | null;
  search: string;
  state: string;
  sort: string;
  direction: string;
  readonly: true;
}

export interface ExplorerFolderDetail {
  path: string;
  relative_path: string;
  entries: Array<{
    name: string;
    path: string;
    relative_path: string;
    size: number;
    extension: string;
    registered: boolean;
    symlink: boolean;
    file: {
      file_id: string;
      canonical_path: string;
      size: number;
      source: string;
      active: number;
      assignment_state: string;
      variant_id: number | null;
      core_title: string | null;
      author: string | null;
      work_bucket_id: number | null;
    } | null;
  }>;
  registered_count: number;
  unregistered_count: number;
  total_size: number;
  truncated: boolean;
  managed_folder: { folder_id: number; work_bucket_id: number; role: string; state: string; work_title: string | null } | null;
  actions: { rename: boolean; move: boolean; quarantine: boolean; future_version: string | null };
  readonly: true;
}

export interface FolderQuarantinePlan {
  version: "1.3.5";
  kind: "user_folder_quarantine";
  item_count: number;
  source_path: string;
  destination_path: string;
  registered_count: number;
  auxiliary_count: number;
  directory_count: number;
  total_size: number;
  work_bucket_ids: number[];
  managed_folders: Array<{ folder_id: number; work_bucket_id: number; canonical_path: string; role: string }>;
  related_folders: Array<{ folder_id: number; work_bucket_id: number; canonical_path: string; role: string; display_title: string | null }>;
  items: Array<{ relative_path: string; source_path: string; size: number; registered: boolean; file_id: string | null }>;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  readonly: true;
}

export interface ExplorerQuarantineItem {
  operation_id: number | null;
  file_id?: string | null;
  keep_file_id?: string | null;
  action: string | null;
  source_path: string | null;
  source_size: number | null;
  keep_path: string | null;
  keep_size: number | null;
  name: string;
  path: string;
  category: string;
  physical_state: "present" | "missing" | "untracked" | "purged";
  size: number | null;
  modified_at: number | null;
  age_days: number | null;
  tracked: boolean;
  restore_available: boolean;
  purge_available: boolean;
  future_version: string | null;
  operation_state?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  related_files: Array<{
    file_id: string;
    name: string;
    path: string;
    size: number;
    bases: string[];
    confidence: "kept" | "confirmed" | "candidate";
  }>;
}

export interface RelationshipPlan {
  version: "1.3.2";
  kind: "relationship";
  item_count: number;
  left: ExplorerFile & Record<string, unknown>;
  right: ExplorerFile & Record<string, unknown>;
  verdict: "same_content" | "same_work_distinct_variant" | "distinct_work";
  variant_kind: string;
  note: string;
  existing_decision: (ExplorerHistoryItem & { decision_id: number; verdict: string }) | null;
  mode: "new" | "correction";
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  readonly: true;
}

export interface QuarantinePlan {
  version: "1.3.2";
  kind: "user_quarantine";
  item_count: 1;
  source: ExplorerFile & Record<string, unknown>;
  keep: (ExplorerFile & Record<string, unknown>) | null;
  replacement_representative: (ExplorerFile & Record<string, unknown>) | null;
  remaining_variant_files: number;
  retired_variant: boolean;
  retired_work: boolean;
  fingerprint_preparation_count: number;
  destination_root: string;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  readonly: true;
}

export interface RestorePlan {
  version: "1.3.2";
  kind: "quarantine_restore";
  item_count: 1;
  operation_id: number;
  source: ExplorerFile & Record<string, unknown>;
  reference: (ExplorerFile & Record<string, unknown>) | null;
  quarantine_path: string;
  destination_path: string;
  verdict: "same_work_distinct_variant" | "distinct_work";
  note: string;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  readonly: true;
}

export interface PurgePlan {
  version: "1.3.2";
  kind: "quarantine_purge";
  item_count: number;
  total_size: number;
  items: Array<{ operation_id: number; file_id: string; name: string; path: string; size: number; keep_path?: string | null; age_days: number | null; blocked_reasons: string[] }>;
  blocked_reasons: string[];
  apply_available: boolean;
  plan_sha256: string;
  irreversible: true;
  readonly: true;
}

export interface ExplorerQuarantineListing {
  items: ExplorerQuarantineItem[];
  total: number;
  limit: number;
  cursor: string | null;
  next_cursor: string | null;
  search: string;
  state: string;
  summary: Record<"present" | "missing" | "untracked" | "purged", number>;
  readonly: true;
}

export interface ReviewQueueItem {
  kind: "database" | "filesystem";
  category: string;
  state: string;
  physical_state: "relation_only" | "quarantined" | "queue_missing";
  review_id?: number;
  candidate_path?: string | null;
  reference_path?: string | null;
  queue_path?: string | null;
  created_at?: string;
  name?: string;
  path?: string;
  size?: number;
  modified_at?: number;
}

export interface ReviewQueueListing {
  items: ReviewQueueItem[];
  total_visible: number;
  summary: Record<"relation_only" | "quarantined" | "queue_missing", number>;
  limit: number;
  search: string;
  category: string;
  physical: string;
  readonly: true;
}
