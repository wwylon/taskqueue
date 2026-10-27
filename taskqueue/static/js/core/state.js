// Shared UI state. app.js reads and writes these through `S` (window.tqState); modules import it directly.
export const S = {
  status: null,
  queue: [],
  history: [],
  cfg: {},
  workers: [],
  queueTotal: 0,
  historyTotal: 0,
  brokenDeps: [],
  brokenDepsBannerDismissedCount: null,
  queuePage: 0,
  forceStarting: {},   // 'project/task' -> epoch ms; set from a confirmed Force until the task runs
  historyPage: 0,
  historyLimit: 20,
  projects: [],
  myProjects: [],       // R8: projects for current operator (mine scope)
  operator: '',         // R8: prefs / X-TQ-Operator (localStorage tq_operator)
  mineOnly: false,      // R8: default true when operator set; View all clears it
  selectedProject: 'all',
  cpuTotalCores: 0,
  queueStatusFilter: 'all',
  historyStatusFilter: 'all',
  queueProjectFilter: 'all',
  queueActiveOnly: false,
  queueNewFailedOnly: false,
  tagFilter: null,
  groupFilter: null,
  allTags: [],
  allTagProjects: {},
  projectRoots: {},
  queueSearch: '',
  savedFilters: [],
  savedToday: false,
  forceQueueRender: false,
  queueSelection: new Set(),
  kbdTaskKey: null,
  drawerTask: null,
  docsCurrentId: null,
  docsSectionId: null,
  applyingUrl: false,
  queueSearchDebounce: null,
  historySearchDebounce: null,
  bootUi: null,
  drawerTab: 'info',
  drawerYamlMtime: null,
  queueExportRows: [],
  bulkBusy: false,
  sseState: 'connecting',
  logSearch: {},
  expandedLogs: {},      // run log panels, key = panel prefix + run_id (d:/q:/qc:/h:)
  logSseMap: {},         // live log EventSources by runId (queue panel)
  cardLogSseMap: {},     // live log EventSources by runId (run cards)
  expandedHistory: {},   // per-task run history panels, key = project/task_id
  _histGen: {},          // F-133: generation tokens for async history panels
  historySort: { field: 'run_id', dir: -1 },
  prevHistoryStr: '',    // owned by refresh.js identity key — do not clobber in renderHistory
  globalWorkerFilter: '',
  historyPageSize: 20,
  queueSort: { field: 'status', dir: 1 },
  prevQueueStr: '',
  queueFlashKey: null,    // key = project + NUL + task_id
  queueFlashTimer: null,
  refreshResolvers: [],
  prevRunningStr: '',     // structural identity of the running set (not live metrics)
  prevGaugeStructStr: '',
  prevWorkersStr: '',
  dashboardEtag: '',
  dashboardEtagUrl: '',
  lastRefreshTime: null,
  lastDataVersion: null,
  dashStatsByKey: {},
  // Per-view state (every S field is declared here; views only assign).
  systemAlerts: [],       // alerts tab: /api/alerts/system rows
  auditRows: [],          // audit tab rows (for export)
  schedKey: '',           // schedule tab: selected row, JSON [project, task_id]
  taskStatsRows: [],      // tasks tab rows
  tasksSearchDebounce: null,
  usageDays: 7,
  usageToggle: {},        // usage legend: key = proj+':gpu' | proj+':cpu' -> false when hidden
  usageChart: null,       // cached series for hover redraw
  usageChartType: 'line',
  bulkRows: {},          // queue: status by selection key for rows selected via 'select all matching'
  drawerSeries: null,     // drawer: TQ_METRIC sparkline samples of the task's latest run ({key: [[ts, v], ...]})
  drawerSeriesAt: 0,
};
