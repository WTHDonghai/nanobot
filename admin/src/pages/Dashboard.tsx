import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import './Pages.css';

type TokenUsage = {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
};

type DailyAnalyticsRow = {
  date: string;
  active_users: number;
  session_count: number;
  message_count: number;
  user_message_count: number;
  assistant_message_count: number;
  tool_call_count: number;
  failed_tool_call_count: number;
  token_usage: TokenUsage;
};

type DailyAnalyticsTotals = Omit<DailyAnalyticsRow, 'date'>;

const localDateIso = (date: Date) => {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
};

const todayIso = () => localDateIso(new Date());

const daysAgoIso = (days: number) => {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return localDateIso(date);
};

const defaultDateRange = () => ({
  fromDate: daysAgoIso(13),
  toDate: todayIso(),
});

const browserTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

const formatNumber = (value?: number) => Number(value || 0).toLocaleString();

const formatPercent = (value: number) => `${value.toFixed(value >= 10 ? 0 : 1)}%`;

const tokenTotal = (usage?: TokenUsage) => usage?.total_tokens || 0;

const safeDivide = (numerator: number, denominator: number) => (
  denominator > 0 ? numerator / denominator : 0
);

const toolSuccessRate = (row: Pick<DailyAnalyticsRow, 'tool_call_count' | 'failed_tool_call_count'>) => (
  row.tool_call_count > 0
    ? ((row.tool_call_count - row.failed_tool_call_count) / row.tool_call_count) * 100
    : 100
);

const formatShortDate = (value: string) => value.slice(5) || value;

const sessionHref = (params: Record<string, string>) => `/sessions?${new URLSearchParams(params).toString()}`;

const rangeLabel = (fromDate: string, toDate: string) => (
  fromDate === toDate ? fromDate : `${fromDate} 至 ${toDate}`
);

const formatYLabel = (num: number) => {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1)}k`;
  return String(Math.round(num));
};

const getBezierPath = (points: Array<{ x: number; y: number }>) => {
  if (points.length === 0) return '';
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;

  let d = `M ${points[0].x} ${points[0].y}`;
  for (let i = 0; i < points.length - 1; i++) {
    const curr = points[i];
    const next = points[i + 1];
    const cpX1 = curr.x + (next.x - curr.x) / 3;
    const cpY1 = curr.y;
    const cpX2 = next.x - (next.x - curr.x) / 3;
    const cpY2 = next.y;
    d += ` C ${cpX1} ${cpY1}, ${cpX2} ${cpY2}, ${next.x} ${next.y}`;
  }
  return d;
};

const getBezierAreaPath = (points: Array<{ x: number; y: number }>, chartHeight: number, paddingTop: number) => {
  if (points.length === 0) return '';
  const linePath = getBezierPath(points);
  const first = points[0];
  const last = points[points.length - 1];
  const bottom = paddingTop + chartHeight;
  return `${linePath} L ${last.x} ${bottom} L ${first.x} ${bottom} Z`;
};

const TrendChartPanel: React.FC<{
  data: DailyAnalyticsRow[];
  totals: DailyAnalyticsTotals | null;
  refreshing: boolean;
}> = ({ data, totals, refreshing }) => {
  const [activeMetrics, setActiveMetrics] = useState<string[]>(['sessions', 'messages']);
  const [scaleMode, setScaleMode] = useState<'absolute' | 'trend'>('trend');
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const [focusedMetric, setFocusedMetric] = useState<string | null>(null);

  const totalTokens = tokenTotal(totals?.token_usage);
  const sessionCount = totals?.session_count || 0;
  const messageCount = totals?.message_count || 0;

  const metricsConfig = useMemo<Array<{
    key: string;
    label: string;
    color: string;
    metric: (row: DailyAnalyticsRow) => number;
    total: number;
  }>>(() => [
    {
      key: 'sessions',
      label: '服务会话',
      color: 'var(--primary)',
      metric: (row) => row.session_count || 0,
      total: sessionCount,
    },
    {
      key: 'messages',
      label: '交互消息',
      color: '#10b981',
      metric: (row) => row.message_count || 0,
      total: messageCount,
    },
    {
      key: 'tokens',
      label: 'Token 消耗',
      color: '#f59e0b',
      metric: (row) => tokenTotal(row.token_usage),
      total: totalTokens,
    },
  ], [sessionCount, messageCount, totalTokens]);

  const activeConfigs = useMemo(() => (
    metricsConfig.filter((cfg) => activeMetrics.includes(cfg.key))
  ), [metricsConfig, activeMetrics]);

  // SVG dimensions
  const width = 680;
  const height = 240;
  const paddingLeft = 50;
  const paddingRight = 20;
  const paddingTop = 25;
  const paddingBottom = 30;
  const chartWidth = width - paddingLeft - paddingRight;
  const chartHeight = height - paddingTop - paddingBottom;
  const pointX = useCallback((index: number) => (
    data.length <= 1
      ? paddingLeft + chartWidth / 2
      : paddingLeft + (index / (data.length - 1)) * chartWidth
  ), [chartWidth, data.length, paddingLeft]);

  // Compute points for all configs, regardless of whether they are active, to prevent flickering during unmounting/remounting
  const seriesPoints = useMemo(() => {
    if (data.length === 0) return [];

    // Calculate the absolute max of all active metrics combined
    const activeValues = activeConfigs.flatMap(c => data.map(c.metric));
    const activeMax = Math.max(...activeValues, 1);

    return metricsConfig.map((cfg) => {
      const values = data.map(cfg.metric);
      let max = 1;

      if (scaleMode === 'trend') {
        max = Math.max(...values, 1);
      } else {
        // Absolute mode: scale against the active max.
        // For inactive metrics, use max(activeMax, selfMax) to prevent overflow while keeping layout aligned.
        const selfMax = Math.max(...values, 1);
        const isActive = activeMetrics.includes(cfg.key);
        max = isActive ? activeMax : Math.max(activeMax, selfMax);
      }

      const points = values.map((val, idx) => {
        const x = pointX(idx);
        const y = paddingTop + chartHeight - (val / max) * chartHeight;
        return { x, y };
      });

      return {
        key: cfg.key,
        color: cfg.color,
        points,
        values,
        max
      };
    });
  }, [data, metricsConfig, activeConfigs, scaleMode, activeMetrics, pointX, chartHeight]);

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    if (data.length === 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const svgMouseX = (mouseX / rect.width) * width;

    let closestIdx = 0;
    let minDiff = Infinity;
    for (let i = 0; i < data.length; i++) {
      const diff = Math.abs(svgMouseX - pointX(i));
      if (diff < minDiff) {
        minDiff = diff;
        closestIdx = i;
      }
    }
    setHoveredIdx(closestIdx);
  };

  const handleMouseLeave = () => {
    setHoveredIdx(null);
  };

  const toggleMetric = (key: string) => {
    setActiveMetrics((prev) => {
      if (prev.includes(key)) {
        if (prev.length === 1) return prev; // Keep at least one active
        return prev.filter((k) => k !== key);
      }
      return [...prev, key];
    });
  };

  const midIdx = Math.floor(data.length / 2);

  const absoluteMax = useMemo(() => {
    if (data.length === 0 || activeConfigs.length === 0) return 1;
    const allActiveValues = activeConfigs.flatMap(c => data.map(c.metric));
    return Math.max(...allActiveValues, 1);
  }, [data, activeConfigs]);

  const activeAvgTokens = useMemo(() => {
    const userQuestions = totals?.user_message_count || 0;
    return userQuestions ? Math.round(totalTokens / userQuestions) : 0;
  }, [totalTokens, totals?.user_message_count]);

  return (
    <div className={`overview-trend-panel ${refreshing ? 'loading-fade' : ''}`}>
      <div className="trend-header">
        <div className="trend-legend">
          {metricsConfig.map((cfg) => {
            const isActive = activeMetrics.includes(cfg.key);
            return (
              <button
                key={cfg.key}
                onClick={() => toggleMetric(cfg.key)}
                onMouseEnter={() => isActive && setFocusedMetric(cfg.key)}
                onMouseLeave={() => setFocusedMetric(null)}
                className={`legend-btn ${isActive ? 'active' : ''} ${cfg.key}`}
              >
                <span className="legend-dot" style={{ background: cfg.color }} />
                <span>{cfg.label}</span>
                <span style={{ opacity: 0.6, fontSize: '0.72rem', marginLeft: '4px' }}>
                  ({formatNumber(cfg.total)})
                </span>
              </button>
            );
          })}
        </div>

        <div className="scale-mode-selector">
          <button
            onClick={() => setScaleMode('trend')}
            className={`scale-btn ${scaleMode === 'trend' ? 'active' : ''}`}
          >
            对比趋势 (独立)
          </button>
          <button
            onClick={() => setScaleMode('absolute')}
            className={`scale-btn ${scaleMode === 'absolute' ? 'active' : ''}`}
          >
            对齐数值 (共享)
          </button>
        </div>
      </div>

      <div style={{ position: 'relative', width: '100%' }}>
        <svg
          viewBox={`0 0 ${width} ${height}`}
          style={{ width: '100%', height: 'auto', display: 'block', overflow: 'visible' }}
          onMouseMove={handleMouseMove}
          onMouseLeave={handleMouseLeave}
        >
          <defs>
            {seriesPoints.map((series) => (
              <linearGradient key={series.key} id={`gradient-${series.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={series.color} stopOpacity="0.25" />
                <stop offset="100%" stopColor={series.color} stopOpacity="0.00" />
              </linearGradient>
            ))}
          </defs>

          {/* Gridlines */}
          {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
            const y = paddingTop + ratio * chartHeight;
            return (
              <line
                key={ratio}
                x1={paddingLeft}
                y1={y}
                x2={width - paddingRight}
                y2={y}
                stroke="rgba(128, 128, 128, 0.08)"
                strokeDasharray="4 4"
              />
            );
          })}

          {/* Y-axis Labels */}
          {scaleMode === 'absolute' ? (
            <>
              <text x={paddingLeft - 8} y={paddingTop + 4} textAnchor="end" fill="var(--muted)" fontSize="9">
                {formatYLabel(absoluteMax)}
              </text>
              <text x={paddingLeft - 8} y={paddingTop + chartHeight / 2 + 3} textAnchor="end" fill="var(--muted)" fontSize="9">
                {formatYLabel(absoluteMax / 2)}
              </text>
              <text x={paddingLeft - 8} y={paddingTop + chartHeight + 3} textAnchor="end" fill="var(--muted)" fontSize="9">
                0
              </text>
            </>
          ) : (
            <>
              <text x={paddingLeft - 8} y={paddingTop + 4} textAnchor="end" fill="var(--muted)" fontSize="9">
                MAX
              </text>
              <text x={paddingLeft - 8} y={paddingTop + chartHeight / 2 + 3} textAnchor="end" fill="var(--muted)" fontSize="9">
                50%
              </text>
              <text x={paddingLeft - 8} y={paddingTop + chartHeight + 3} textAnchor="end" fill="var(--muted)" fontSize="9">
                0
              </text>
            </>
          )}

          {/* Gradient fills underneath the lines */}
          {seriesPoints.map((series) => {
            const isActive = activeMetrics.includes(series.key);
            const isFocused = focusedMetric === series.key;
            const hasFocus = focusedMetric !== null;

            let areaOpacity = 0;
            if (isActive) {
              if (hasFocus) {
                areaOpacity = isFocused ? 0.32 : 0.04;
              } else {
                areaOpacity = 0.20;
              }
            }

            const areaPath = getBezierAreaPath(series.points, chartHeight, paddingTop);
            return (
              <path
                key={`area-${series.key}`}
                d={areaPath}
                fill={`url(#gradient-${series.key})`}
                opacity={areaOpacity}
                style={{
                  transition: 'opacity 0.25s ease-in-out, d 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                  pointerEvents: 'none',
                }}
              />
            );
          })}

          {/* Smooth Bezier Lines */}
          {seriesPoints.map((series) => {
            const isActive = activeMetrics.includes(series.key);
            const isFocused = focusedMetric === series.key;
            const hasFocus = focusedMetric !== null;

            let lineOpacity = 0;
            let strokeWidth = 0;
            if (isActive) {
              if (hasFocus) {
                lineOpacity = isFocused ? 1 : 0.20;
                strokeWidth = isFocused ? 4 : 1.5;
              } else {
                lineOpacity = 1;
                strokeWidth = 3;
              }
            }

            const linePath = getBezierPath(series.points);
            return (
              <path
                key={`line-${series.key}`}
                d={linePath}
                fill="none"
                stroke={series.color}
                strokeWidth={strokeWidth}
                strokeLinecap="round"
                strokeLinejoin="round"
                opacity={lineOpacity}
                style={{
                  transition: 'opacity 0.25s ease-in-out, stroke-width 0.25s ease-in-out, d 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                  pointerEvents: 'none',
                }}
              />
            );
          })}

          {/* Thick Invisible Trigger Paths for easy line hovering */}
          {seriesPoints.map((series) => {
            const isActive = activeMetrics.includes(series.key);
            const linePath = getBezierPath(series.points);
            return (
              <path
                key={`trigger-${series.key}`}
                d={linePath}
                fill="none"
                stroke="transparent"
                strokeWidth="16"
                style={{
                  cursor: isActive ? 'pointer' : 'default',
                  pointerEvents: isActive ? 'auto' : 'none',
                }}
                onMouseEnter={() => setFocusedMetric(series.key)}
                onMouseLeave={() => setFocusedMetric(null)}
              />
            );
          })}

          {/* X-axis Date Labels */}
          {data.length > 0 && (
            <>
              <text x={paddingLeft} y={height - 8} textAnchor="start" fill="var(--muted)" fontSize="9">
                {formatShortDate(data[0].date)}
              </text>
              {data.length > 2 && (
                <text x={paddingLeft + chartWidth / 2} y={height - 8} textAnchor="middle" fill="var(--muted)" fontSize="9">
                  {formatShortDate(data[midIdx].date)}
                </text>
              )}
              <text x={width - paddingRight} y={height - 8} textAnchor="end" fill="var(--muted)" fontSize="9">
                {formatShortDate(data[data.length - 1].date)}
              </text>
            </>
          )}

          {/* Guide Line and interactive circles */}
          {hoveredIdx !== null && data[hoveredIdx] && (
            <>
              <line
                x1={pointX(hoveredIdx)}
                y1={paddingTop}
                x2={pointX(hoveredIdx)}
                y2={paddingTop + chartHeight}
                stroke="rgba(128, 128, 128, 0.25)"
                strokeWidth="1.2"
                strokeDasharray="3 3"
              />
              {seriesPoints.map((series) => {
                const isActive = activeMetrics.includes(series.key);
                const isFocused = focusedMetric === series.key;
                const hasFocus = focusedMetric !== null;
                const pt = series.points[hoveredIdx];
                if (!pt) return null;

                let dotOpacity = 0;
                if (isActive) {
                  if (hasFocus) {
                    dotOpacity = isFocused ? 1 : 0.15;
                  } else {
                    dotOpacity = 1;
                  }
                }

                return (
                  <g key={series.key} style={{
                    opacity: dotOpacity,
                    transition: 'opacity 0.25s ease-in-out',
                  }}>
                    <circle
                      cx={pt.x}
                      cy={pt.y}
                      r={isActive ? "7.5" : "0"}
                      fill={series.color}
                      opacity="0.25"
                      style={{ transition: 'r 0.3s ease-in-out' }}
                    />
                    <circle
                      cx={pt.x}
                      cy={pt.y}
                      r={isActive ? "4.5" : "0"}
                      fill={series.color}
                      stroke="var(--bg2)"
                      strokeWidth="2"
                      style={{ transition: 'r 0.3s ease-in-out' }}
                    />
                  </g>
                );
              })}
            </>
          )}
        </svg>

        {/* Floating Tooltip */}
        {hoveredIdx !== null && data[hoveredIdx] && (
          <div
            className="trend-tooltip"
            style={{
              left: `${(pointX(hoveredIdx) / width) * 100}%`,
            }}
          >
            <div style={{ color: 'var(--muted)', fontSize: '0.68rem', fontWeight: 600, borderBottom: '1px solid var(--border)', paddingBottom: '3px', marginBottom: '2px' }}>
              {data[hoveredIdx].date}
            </div>
            {activeConfigs.map((cfg) => {
              const val = cfg.metric(data[hoveredIdx]);
              return (
                <div key={cfg.key} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: cfg.color }} />
                    <span style={{ color: 'var(--light)' }}>{cfg.label}</span>
                  </div>
                  <strong style={{ color: cfg.color }}>
                    {formatNumber(val)}
                    {cfg.key === 'tokens' && hoveredIdx !== null && (
                      <span style={{ fontSize: '0.62rem', fontWeight: 'normal', color: 'var(--muted)', marginLeft: '4px' }}>
                        (均值: {formatNumber(data[hoveredIdx].user_message_count ? Math.round(val / data[hoveredIdx].user_message_count) : 0)})
                      </span>
                    )}
                  </strong>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
};

const ValueStatCard: React.FC<{
  label: string;
  value: string;
  caption: string;
  tone?: 'primary' | 'success' | 'warning';
  to?: string;
}> = ({ label, value, caption, tone, to }) => {
  const content = (
    <>
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone || ''}`}>{value}</div>
      <div className="stat-caption">{caption}</div>
    </>
  );

  if (to) {
    return (
      <Link to={to} className="stat-card value-stat-card interactive" style={{ textDecoration: 'none', display: 'block' }}>
        {content}
      </Link>
    );
  }

  return (
    <div className="stat-card value-stat-card">
      {content}
    </div>
  );
};

const RootDashboard: React.FC = () => {
  const { serverUrl, apiKey } = useAuth();
  const [data, setData] = useState<any>({ accounts: [], health: {}, ready: {} });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    const fetchData = async () => {
      try {
        const [accRes, healthRes, readyRes] = await Promise.all([
          fetchApi(serverUrl, apiKey, '/api/v1/admin/accounts').catch(() => ({ result: [] })),
          fetchApi(serverUrl, apiKey, '/health', { method: 'GET' }).catch(() => ({})),
          fetchApi(serverUrl, apiKey, '/ready', { method: 'GET' }).catch(() => ({}))
        ]);
        if (mounted) {
          setData({ accounts: accRes.result || [], health: healthRes, ready: readyRes });
          setLoading(false);
        }
      } catch (err) {
        if (mounted) setLoading(false);
      }
    };
    fetchData();
    return () => { mounted = false; };
  }, [serverUrl, apiKey]);

  if (loading) return <div className="empty"><div className="loader"></div></div>;

  const { accounts, health, ready } = data;
  const userCount = accounts.reduce((acc: number, cur: any) => acc + (cur.user_count || 0), 0);
  const version = health.version || '-';
  const checks = ready.checks || {};
  const isReady = ready.status === 'ready';

  return (
    <div>
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">租户总数 (Accounts)</div>
          <div className="stat-value primary">{accounts.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">全局用户数 (Users)</div>
          <div className="stat-value">{userCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">系统版本</div>
          <div className="stat-value" style={{ fontSize: '1.1rem', color: 'var(--light)' }}>{version}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">部署状态</div>
          <div className={`stat-value ${isReady ? 'success' : 'warning'}`} style={{ fontSize: '1.1rem' }}>
            {isReady ? '就绪' : '异常'}
          </div>
        </div>
      </div>
      
      <div className="section-title">
        <svg width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" style={{ marginRight: 8 }}>
          <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
        </svg>
        底座子系统状态
      </div>
      <div className="health-grid">
        {Object.entries(checks).map(([k, v]) => (
          <div className="card" key={k}>
            <div className="health-card-title">{k}</div>
            <div className={`health-card-value ${v === 'ok' ? 'health-ok' : v === 'not_configured' ? 'health-warn' : 'health-err'}`}>
              {String(v)}
            </div>
          </div>
        ))}
      </div>
      
      <hr className="divider" />
      <div className="section-title">近期创建账号</div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Account ID</th><th>创建时间</th><th>用户数</th></tr>
          </thead>
          <tbody>
            {accounts.slice(0, 5).map((a: any) => (
              <tr key={a.account_id}>
                <td><code style={{ color: 'var(--primary)' }}>{a.account_id}</code></td>
                <td style={{ color: 'var(--muted)' }}>{a.created_at ? new Date(a.created_at).toLocaleString() : '-'}</td>
                <td>{a.user_count}</td>
              </tr>
            ))}
            {accounts.length === 0 && <tr><td colSpan={3} className="empty">暂无账号</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const TenantDashboard: React.FC = () => {
  const { serverUrl, apiKey, accountId } = useAuth();
  const [data, setData] = useState<any>({ users: [], health: {}, ready: {}, daily: [], totals: null });
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [fromDate, setFromDate] = useState(() => defaultDateRange().fromDate);
  const [toDate, setToDate] = useState(() => defaultDateRange().toDate);
  const [selectedDate, setSelectedDate] = useState('');

  useEffect(() => {
    let mounted = true;
    if (!loading) setRefreshing(true);
    const fetchData = async () => {
      try {
        const encodedAccountId = encodeURIComponent(accountId || '');
        const analyticsParams = new URLSearchParams({
          from_date: fromDate,
          to_date: toDate,
          tz: browserTimeZone(),
        });
        const analyticsPath = `/api/v1/admin/accounts/${encodedAccountId}/analytics/daily?${analyticsParams.toString()}`;
        const [usersRes, healthRes, readyRes, analyticsRes] = await Promise.all([
          fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${encodedAccountId}/users`).catch(() => ({ result: [] })),
          fetchApi(serverUrl, apiKey, '/health', { method: 'GET' }).catch(() => ({})),
          fetchApi(serverUrl, apiKey, '/ready', { method: 'GET' }).catch(() => ({})),
          fetchApi(serverUrl, apiKey, analyticsPath).catch(() => ({ result: { daily: [], totals: null } }))
        ]);
        if (mounted) {
          setData({
            users: usersRes.result || [],
            health: healthRes,
            ready: readyRes,
            daily: analyticsRes.result?.daily || [],
            totals: analyticsRes.result?.totals || null,
          });
          setLoading(false);
          setRefreshing(false);
        }
      } catch (err) {
        if (mounted) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    };
    if (accountId) fetchData();
    return () => { mounted = false; };
  }, [serverUrl, apiKey, accountId, fromDate, toDate]);

  if (loading) return <div className="empty"><div className="loader"></div></div>;

  const { users, health, ready } = data;
  const daily = data.daily as DailyAnalyticsRow[];
  const totals = data.totals as DailyAnalyticsTotals | null;
  const version = health.version || '-';
  const checks = ready.checks || {};
  const isReady = ready.status === 'ready';
  const sessionCount = totals?.session_count || 0;
  const totalTokens = tokenTotal(totals?.token_usage);
  const toolCallCount = totals?.tool_call_count || 0;
  const failedToolCount = totals?.failed_tool_call_count || 0;
  const avgTurnsPerSession = sessionCount
    ? ((totals?.user_message_count || 0) / sessionCount).toFixed(1)
    : '0';
  const hasUsageData = sessionCount > 0;
  const adminCount = users.filter((u: any) => u.role === 'admin').length;
  const activeUsers = totals?.active_users || 0;
  const userQuestions = totals?.user_message_count || 0;
  const assistantReplies = totals?.assistant_message_count || 0;
  const totalToolSuccessRate = toolCallCount ? ((toolCallCount - failedToolCount) / toolCallCount) * 100 : 100;
  const avgQuestionsPerUser = safeDivide(userQuestions, activeUsers);
  const latestDaily = [...daily].reverse().find((row) => row.session_count > 0) || daily[daily.length - 1] || null;
  const selectedDaily = daily.find((row) => row.date === selectedDate) || latestDaily;
  const dailyRangeStart = daily[0]?.date || '';
  const dailyRangeEnd = daily[daily.length - 1]?.date || '';
  const selectedDateInRange = Boolean(selectedDate && daily.some((row) => row.date === selectedDate));
  const selectedDailyTokens = tokenTotal(selectedDaily?.token_usage);
  const selectedDailyQuestions = selectedDaily?.user_message_count || 0;
  const selectedDailyToolCalls = selectedDaily?.tool_call_count || 0;
  const selectedDailyFailedTools = selectedDaily?.failed_tool_call_count || 0;
  const selectedDailySuccessRate = selectedDaily ? toolSuccessRate(selectedDaily) : 100;

  return (
    <div>
      <div className="stats-grid overview-kpi-grid">
        <ValueStatCard
          label="服务用户"
          value={formatNumber(activeUsers)}
          caption={`${rangeLabel(fromDate, toDate)}，${formatNumber(sessionCount)} 个会话`}
          tone="primary"
          to={sessionHref({ from_date: fromDate, to_date: toDate })}
        />
        <ValueStatCard
          label="承接问题"
          value={formatNumber(userQuestions)}
          caption={`平均 ${avgQuestionsPerUser.toFixed(avgQuestionsPerUser >= 10 ? 0 : 1)} 问/用户`}
          to={sessionHref({ from_date: fromDate, to_date: toDate })}
        />
        <ValueStatCard
          label="自动响应"
          value={formatNumber(assistantReplies)}
          caption={`平均 ${avgTurnsPerSession} 轮/会话`}
          to={sessionHref({ from_date: fromDate, to_date: toDate, sort_by: 'message_count', sort_order: 'desc' })}
        />
        <ValueStatCard
          label="工具成功率"
          value={toolCallCount ? formatPercent(totalToolSuccessRate) : '-'}
          caption={`${formatNumber(toolCallCount)} 次工具调用`}
          tone={failedToolCount > 0 ? 'warning' : 'success'}
          to={failedToolCount > 0 ? sessionHref({ from_date: fromDate, to_date: toDate, sort_by: 'failed_tool_call_count', sort_order: 'desc' }) : undefined}
        />
      </div>


      <div className="daily-drilldown">
        <div className="daily-picker-panel">
          <div className="daily-picker-head">
            <div>
              <div className="section-title">区间查询</div>
              <div className="daily-detail-subtitle">{rangeLabel(fromDate, toDate)}</div>
            </div>
            <Link
              className="btn btn-ghost btn-sm"
              to={sessionHref({
                from_date: fromDate,
                to_date: toDate,
                sort_by: 'last_active',
                sort_order: 'desc',
              })}
            >
              查看区间会话
            </Link>
          </div>

          {/* Quick Date Presets */}
          <div style={{ display: 'flex', gap: '6px', marginBottom: '14px', flexWrap: 'wrap', alignItems: 'center' }}>
            <span style={{ color: 'var(--muted)', fontSize: '0.72rem', marginRight: '4px', fontWeight: 500 }}>快速筛选:</span>
            {[
              { label: '今天', days: 0 },
              { label: '最近 7 天', days: 6 },
              { label: '最近 14 天', days: 13 },
              { label: '最近 30 天', days: 29 },
            ].map((preset) => {
              const presetFrom = daysAgoIso(preset.days);
              const presetTo = todayIso();
              const isSelected = fromDate === presetFrom && toDate === presetTo;
              return (
                <button
                  key={preset.label}
                  type="button"
                  onClick={() => {
                    setFromDate(presetFrom);
                    setToDate(presetTo);
                    setSelectedDate('');
                  }}
                  style={{
                    background: isSelected ? 'var(--primary-dim)' : 'transparent',
                    border: `1px solid ${isSelected ? 'var(--primary)' : 'var(--border)'}`,
                    color: isSelected ? 'var(--text)' : 'var(--muted)',
                    borderRadius: '20px',
                    padding: '3px 10px',
                    fontSize: '0.7rem',
                    fontWeight: 600,
                    cursor: 'pointer',
                    transition: 'all 0.2s',
                  }}
                >
                  {preset.label}
                </button>
              );
            })}
          </div>

          <div className="date-range-controls">
            <div className="form-group">
              <label>开始日期</label>
              <input
                className="input daily-date-input"
                max={toDate}
                onChange={(event) => {
                  setFromDate(event.target.value);
                  setSelectedDate('');
                }}
                type="date"
                value={fromDate}
              />
            </div>
            <div className="form-group">
              <label>结束日期</label>
              <input
                className="input daily-date-input"
                min={fromDate}
                onChange={(event) => {
                  setToDate(event.target.value);
                  setSelectedDate('');
                }}
                type="date"
                value={toDate}
              />
            </div>
          </div>

          <div className="daily-picker-subhead">
            <span>区间内按日钻取</span>
            {selectedDaily && selectedDateInRange && <strong>{selectedDaily.date}</strong>}
          </div>

          <div className={`daily-day-list ${refreshing ? 'loading-fade' : ''}`} role="list">
            {daily.length > 0 ? (
              daily.map((row) => {
                const isSelected = selectedDaily?.date === row.date;
                return (
                  <button
                    aria-pressed={isSelected}
                    className={`daily-day-item ${isSelected ? 'active' : ''} ${row.session_count > 0 ? 'has-data' : ''}`}
                    key={row.date}
                    onClick={() => setSelectedDate(row.date)}
                    type="button"
                  >
                    <span>{formatShortDate(row.date)}</span>
                    <strong>{formatNumber(row.user_message_count)}</strong>
                    <small>{formatNumber(row.session_count)} 会话</small>
                  </button>
                );
              })
            ) : (
              <div className="daily-empty-placeholder" style={{ gridColumn: 'span 2', padding: '24px 0', textAlign: 'center', color: 'var(--muted)', fontSize: '0.8rem' }}>
                当前筛选区间暂无趋势数据
              </div>
            )}
          </div>
        </div>

        <div className="daily-detail-panel">
          <div className="daily-detail-head">
            <div>
              <div className="section-title">{selectedDaily?.date || '日期详情'}</div>
              <div className="daily-detail-subtitle">
                {selectedDaily && selectedDaily.session_count > 0 ? `${formatNumber(selectedDaily.user_message_count)} 个用户问题，${formatNumber(selectedDaily.assistant_message_count)} 条自动响应` : '所选日期暂无真实使用数据'}
              </div>
            </div>
            {selectedDaily && (
              <Link
                className="btn btn-ghost btn-sm"
                to={sessionHref({
                  from_date: selectedDaily.date,
                  to_date: selectedDaily.date,
                  sort_by: 'last_active',
                  sort_order: 'desc',
                })}
              >
                查看会话
              </Link>
            )}
          </div>

          {selectedDaily ? (
            <>
              <div className="daily-detail-grid">
                <div><span>活跃用户</span><strong>{formatNumber(selectedDaily.active_users)}</strong></div>
                <div><span>会话数</span><strong>{formatNumber(selectedDaily.session_count)}</strong></div>
                <div><span>承接问题</span><strong>{formatNumber(selectedDaily.user_message_count)}</strong></div>
                <div><span>自动响应</span><strong>{formatNumber(selectedDaily.assistant_message_count)}</strong></div>
                <div><span>工具成功率</span><strong className={selectedDailyFailedTools > 0 ? 'warning' : 'success'}>{selectedDailyToolCalls ? formatPercent(selectedDailySuccessRate) : '-'}</strong></div>
                <div><span>Token / 问题</span><strong>{formatNumber(Math.round(safeDivide(selectedDailyTokens, selectedDailyQuestions)))}</strong></div>
              </div>

              {selectedDailyFailedTools > 0 && (
                <div className="daily-action-row">
                  <Link
                    className="daily-action-link danger"
                    to={sessionHref({
                      from_date: selectedDaily.date,
                      to_date: selectedDaily.date,
                      sort_by: 'failed_tool_call_count',
                      sort_order: 'desc',
                    })}
                  >
                    {formatNumber(selectedDailyFailedTools)} 次工具失败
                  </Link>
                </div>
              )}

              {hasUsageData ? (
                <div style={{ borderTop: '1px solid var(--border)', paddingTop: '20px', marginTop: '20px' }}>
                  <div className="section-title" style={{ fontSize: '0.9rem', marginBottom: '12px' }}>区间趋势图</div>
                  <TrendChartPanel data={daily} totals={totals} refreshing={refreshing} />
                </div>
              ) : (
                <div className="empty overview-empty" style={{ marginTop: '20px', minHeight: '120px' }}>
                  所选区间暂无真实会话数据。
                </div>
              )}
            </>
          ) : (
            <div className="empty overview-empty">暂无可查看的日期。</div>
          )}
        </div>
      </div>

      <div className="overview-status-strip" aria-label="工作区运行状态">
        <div>
          <span>区间活跃用户</span>
          <strong>{formatNumber(totals?.active_users)}</strong>
        </div>
        <div>
          <span>模型连接</span>
          <strong className={isReady ? 'success' : 'warning'}>{isReady ? '就绪' : '不可用'}</strong>
        </div>
        <div>
          <span>服务器版本</span>
          <strong>{version}</strong>
        </div>
        <div>
          <span>Admin</span>
          <strong>{formatNumber(adminCount)}</strong>
        </div>
      </div>
    </div>
  );
};

const Dashboard: React.FC = () => {
  const { role } = useAuth();
  if (role === 'root') {
    return <RootDashboard />;
  }
  return <TenantDashboard />;
};

export default Dashboard;
