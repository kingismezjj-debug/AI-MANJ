export type StageMap = Record<string, string>;

export interface ShotCard {
  id: string;
  number: number;
  description: string;
  dialogue: string;
  speaker: string;
  camera: string;
  duration_sec: number;
  location: string;
  stages: StageMap;
  thumbnail_url: string | null;
  preview_url: string | null;
  reference_count: number;
}

export interface EpisodeCard {
  id: string;
  number: number;
  title: string;
  hook: string;
  cliffhanger: string;
  synopsis: string;
  status: string;
  shots: ShotCard[];
}

export interface CharacterCard {
  id: string;
  name: string;
  description: string;
  appearance: string;
  voice_hint: string;
  seed: number;
  status: string;
  locked: boolean;
  image_url: string | null;
}

export interface CostCall {
  id: string;
  category: string;
  provider: string;
  model: string;
  cost_usd: number;
  status: string;
  latency_ms: number;
  error: string | null;
}

export interface Costs {
  actual_usd: number;
  estimated_usd: number;
  reference_usd: number;
  preliminary: boolean;
  by_category: Record<string, { usd: number; calls: number }>;
  calls: CostCall[];
  note: string;
}

export interface Board {
  project: {
    id: string;
    title: string;
    status: string;
    phase: string;
    style: string;
    aspect_ratio: string;
    width: number;
    height: number;
    error: string | null;
    final_video_url: string | null;
    subtitle_url: string | null;
    final_meta: { burned_subtitles?: boolean; duration_sec?: number; ai_label?: boolean };
    created_at: string | null;
  };
  episodes: EpisodeCard[];
  characters: CharacterCard[];
  locations: { id: string; name: string; description: string; image_url: string | null; status: string }[];
  counts: Record<string, number>;
  activity: { stage: string; label: string; entity_type: string; entity_id: string } | null;
  recent_jobs: { id: string; stage: string; label: string; status: string; error: string | null }[];
  costs: Costs;
}

export interface ProjectSummary {
  id: string;
  title: string;
  status: string;
  phase: string;
  error: string | null;
  created_at: string | null;
  settings?: { require_bible_approval?: boolean };
  costs?: { actual_usd: number; reference_usd: number };
}

export const SHOT_STAGES: { key: string; label: string }[] = [
  { key: "storyboard", label: "分镜" },
  { key: "animatic", label: "预演" },
  { key: "video", label: "视频" },
  { key: "voice", label: "配音" },
  { key: "lipsync", label: "口型" },
  { key: "shot_qc", label: "质检" },
];

export function money(value: number | undefined): string {
  return `$${(value || 0).toFixed(3)}`;
}

export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: "草稿",
    running: "制作中",
    awaiting_approval: "待定妆",
    completed: "已成片",
    failed: "失败",
    queued: "排队",
    blocked: "等待",
    succeeded: "完成",
    pending: "待开始",
    generated: "待确认",
    approved: "已锁定",
    assembled: "已合成",
    retry: "重试",
  };
  return map[status] || status;
}
