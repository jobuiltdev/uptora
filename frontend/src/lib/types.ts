export type MonitorType = "HTTP" | "BROWSER" | "FLOW";
export type FlowFieldType =
  "TEXT" | "EMAIL" | "TEXTAREA" | "CHECKBOX" | "SELECT";
export type Health =
  "HEALTHY" | "PROBLEM" | "DEGRADED" | "UNKNOWN" | "PAUSED" | "DISABLED";
export type IncidentStatus = "OPEN" | "RESOLVED";
export interface User {
  id: number;
  email: string;
  first_name: string;
  last_name: string;
  date_joined: string;
}
export interface Website {
  id: number;
  owner: number;
  name: string;
  url: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}
export interface FlowField {
  id?: number;
  selector: string;
  field_type: FlowFieldType;
  value: string;
  position: number;
}
export interface FlowConfig {
  flow_kind: "CONTACT_FORM";
  submit_selector: string;
  success_text: string | null;
  success_selector: string | null;
  success_url_contains: string | null;
  fields: FlowField[];
}
export interface Monitor {
  id: number;
  website: number;
  monitor_type: MonitorType;
  is_enabled: boolean;
  interval_seconds: number;
  timeout_seconds: number;
  expected_text: string | null;
  expected_selector: string | null;
  flow_config?: FlowConfig | null;
  has_open_incident: boolean;
  next_check_at: string | null;
  last_check_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface CheckResult {
  id: number;
  monitor: number;
  checked_at: string;
  is_success: boolean;
  status_code: number | null;
  response_time_ms: number | null;
  error_type: string | null;
  error_message: string | null;
  final_url: string | null;
  screenshot: string | null;
  ssl_expires_at: string | null;
  ssl_days_remaining: number | null;
}
export interface Incident {
  id: number;
  monitor: number;
  website: number;
  website_name: string;
  website_url: string;
  status: IncidentStatus;
  started_at: string;
  resolved_at: string | null;
  duration_seconds: number | null;
  failure_type: string;
  initial_error_message: string | null;
  latest_error_message: string | null;
  initial_status_code: number | null;
  latest_status_code: number | null;
  failure_count: number;
  recovery_count: number;
  created_at: string;
  updated_at: string;
}
export interface IncidentShare {
  created_at: string;
  expires_at: string | null;
  include_evidence: boolean;
  share_url: string;
}
export interface PublicIncidentTimelineEntry {
  occurred_at: string;
  outcome: "FAILURE" | "RECOVERY";
  label: string;
  summary: string;
  status_code: number | null;
  evidence_available: boolean;
}
export interface PublicIncidentReport {
  website_name: string;
  website_url: string;
  monitor_type: MonitorType;
  status: IncidentStatus;
  started_at: string;
  resolved_at: string | null;
  duration_seconds: number;
  failure_type: string;
  failure_summary: string;
  latest_failure_summary: string;
  status_code: number | null;
  failure_count: number;
  recovery_count: number;
  timeline: PublicIncidentTimelineEntry[];
  timeline_total_count: number;
  evidence_available: boolean;
  captured_at: string;
}
export interface Page<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}
export interface DashboardWebsite {
  id: number;
  name: string;
  url: string;
  is_active: boolean;
  health: Health;
  monitor_count: number;
  enabled_monitor_count: number;
  monitor_types: MonitorType[];
  active_incident_count: number;
  last_checked_at: string | null;
  next_check_at: string | null;
}
export interface DashboardIncident {
  id: number;
  monitor: number;
  monitor_type: MonitorType;
  website: number;
  website_name: string;
  website_url: string;
  status: "OPEN";
  started_at: string;
  failure_type: string;
  latest_error_message: string | null;
  failure_count: number;
}
export interface DashboardCheck {
  id: number;
  monitor: number;
  monitor_type: MonitorType;
  website: number;
  website_name: string;
  is_success: boolean;
  status_code: number | null;
  response_time_ms: number | null;
  error_type: string | null;
  error_message: string | null;
  checked_at: string;
  has_evidence: boolean;
}
export interface Dashboard {
  summary: {
    websites: number;
    enabled_monitors: number;
    healthy_monitors: number;
    active_incidents: number;
    checks_24h: number;
  };
  websites: DashboardWebsite[];
  active_incidents: DashboardIncident[];
  recent_checks: DashboardCheck[];
}
export interface NotificationPreference {
  email_enabled: boolean;
  alert_email: string | null;
  created_at: string;
  updated_at: string;
}
