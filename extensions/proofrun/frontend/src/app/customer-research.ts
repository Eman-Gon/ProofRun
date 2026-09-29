export interface CustomerResearchRequest {
  clientNonce: string;
  domain: string;
  month: string;
}

export interface CustomerResearch {
  schema_version: 'proofrun.research.v1';
  research_id: string;
  request_id: string;
  status: 'completed' | 'unavailable' | 'no_data';
  domain: string;
  period: { start_date: string; end_date: string };
  observed_at: string;
  provider: 'similarweb';
  metrics: { name: string; value: number; unit: string }[];
  sources: { title: string; url: string }[];
  limitations: string[];
  error?: { code: string; message: string } | null;
}

export function previousCompleteMonth(now = new Date()): string {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1)).toISOString().slice(0, 7);
}

export function researchRequest(domain: string, month: string, clientNonce: string, now = new Date()): CustomerResearchRequest {
  const normalized = domain.trim().toLowerCase().replace(/^www\./, '');
  if (normalized.length > 253 || !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(normalized)
      || /\.(?:local|localhost|internal|invalid|test|example)$/.test(normalized)) {
    throw new Error('Enter a public website domain, such as similarweb.com, without a URL, path or port.');
  }
  if (month.length !== 7 || !/^\d{4}-(?:0[1-9]|1[0-2])$/.test(month) || month < '2000-01' || month > previousCompleteMonth(now)) {
    throw new Error('Choose a completed month from January 2000 onward.');
  }
  return { clientNonce, domain: normalized, month };
}

export function researchStatusText(research: CustomerResearch): string {
  switch (research.status) {
    case 'completed': return 'Customer context retrieved';
    case 'no_data': return 'No Similarweb data for this domain and month';
    default: return 'Customer research unavailable';
  }
}

export function metricLabel(name: string): string {
  return ({ estimated_visits: 'Estimated website visits' } as Record<string, string>)[name]
    ?? name.replaceAll('_', ' ');
}

// Source URLs are data. Only ordinary HTTPS links can become clickable in the page or export.
export function safeResearchSource(url: string): string | undefined {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' && !parsed.username && !parsed.password ? parsed.href : undefined;
  } catch { return undefined; }
}

const markdownText = (value: string): string => value.replace(/[\r\n]/g, ' ').replace(/([\\`*_{}\[\]()<>!#|])/g, '\\$1');

export function customerResearchBrief(research: CustomerResearch): string[] {
  const lines = [
    '', '## Customer research — Similarweb', '',
    'Customer context only. Website estimates do not establish customer requirements or change the verification or repair verdict.', '',
    `- Status: ${researchStatusText(research)} (${research.status})`,
    `- Domain: ${markdownText(research.domain)}`,
    `- Period: ${markdownText(research.period.start_date)} to ${markdownText(research.period.end_date)}`,
    `- Retrieved at: ${markdownText(research.observed_at)}`,
    `- Provider: ${research.provider}; research ID: ${markdownText(research.research_id)}`,
  ];
  if (research.status === 'completed') {
    lines.push(...research.metrics.map(metric => `- ${markdownText(metricLabel(metric.name))}: ${metric.value.toLocaleString('en-US')} ${markdownText(metric.unit)}`));
  }
  if (research.error) lines.push(`- Availability: ${markdownText(research.error.message)}`);
  lines.push(...research.limitations.map(limit => `- Limitation: ${markdownText(limit)}`));
  for (const source of research.sources) {
    const url = safeResearchSource(source.url);
    if (url) lines.push(`- Source: [${markdownText(source.title)}](<${url.replaceAll('>', '%3E').replaceAll('<', '%3C')}>)`);
  }
  return lines;
}
