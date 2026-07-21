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
