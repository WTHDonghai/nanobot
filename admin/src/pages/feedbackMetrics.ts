export type FeedbackMetricSource = {
  assistant_message_count?: number;
  feedback_count?: number;
  positive_feedback_count?: number;
  negative_feedback_count?: number;
} | null | undefined;

export type FeedbackMetricRates = {
  explicitPositiveRate: number | null;
  feedbackCoverageRate: number | null;
  negativeFeedbackRate: number | null;
  appreciationRate: number | null;
};

const safeCount = (value?: number) => Number(value || 0);

const safeRate = (numerator: number, denominator: number) => (
  denominator > 0 ? numerator / denominator : null
);

export const feedbackMetricRates = (source: FeedbackMetricSource): FeedbackMetricRates => {
  const assistantReplies = safeCount(source?.assistant_message_count);
  const feedbackCount = safeCount(source?.feedback_count);
  const positiveFeedbackCount = safeCount(source?.positive_feedback_count);
  const negativeFeedbackCount = safeCount(source?.negative_feedback_count);

  return {
    explicitPositiveRate: safeRate(positiveFeedbackCount, feedbackCount),
    feedbackCoverageRate: safeRate(feedbackCount, assistantReplies),
    negativeFeedbackRate: safeRate(negativeFeedbackCount, assistantReplies),
    appreciationRate: safeRate(positiveFeedbackCount, assistantReplies),
  };
};

export const formatFeedbackRate = (rate: number | null) => {
  if (rate === null) return '-';
  const percent = rate * 100;
  return `${percent.toFixed(percent >= 10 ? 0 : 1)}%`;
};
