export type ResearchIntentDraft = {
  target_venue?: string;
  target_quality?: string;
  domain?: string;
  expected_output?: string;
  convergence_policy?: {
    min_reviews?: number;
    accept_min_overall?: number;
    accept_min_dimension?: number;
    autonomous_auto_accept?: boolean;
  };
};

export function researchIntentFingerprint(intent: ResearchIntentDraft) {
  return JSON.stringify({
    target_venue: intent.target_venue ?? null,
    target_quality: intent.target_quality ?? null,
    domain: intent.domain ?? null,
    expected_output: intent.expected_output ?? null,
    convergence_policy: {
      min_reviews: intent.convergence_policy?.min_reviews ?? null,
      accept_min_overall: intent.convergence_policy?.accept_min_overall ?? null,
      accept_min_dimension: intent.convergence_policy?.accept_min_dimension ?? null,
      autonomous_auto_accept: intent.convergence_policy?.autonomous_auto_accept ?? null,
    },
  });
}
