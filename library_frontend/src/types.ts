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
  destination_path: string;
  after_core_title: string;
  after_readable_title: string;
  after_query_title: string;
  after_author: string | null;
  after_effective_max: number;
  after_unit: string;
  after_complete: boolean;
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
}

export interface DashboardData {
  version: string;
  database: {
    integrity: string;
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
  jobs: JobRecord[];
}
