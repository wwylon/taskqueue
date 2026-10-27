// ES-module entry. app.js (classic script, loaded after this) still owns most state and views and
// calls these helpers by their old global names, so they are exposed on window as shims.
// Migrate callers to imports as more of app.js moves into modules.
import { projColor, projStyle } from './util/projcolor.js';
import { escHtml, jsq, taskKey, qrowId, domIdPart, fmt, dur, appendLog, LOG_CAP_CHARS } from './util/format.js';
import { tqEventSource, fetchJson, fetchJsonEtag, tqPerfEnabled, tqDominantTimingPhase } from './core/api.js';
import { act, installActions, registerActions, THIS, EVENT } from './core/actions.js';
import { applyLogSearch, searchLog, logSearchNav, openLogStream, logTailText,
  toggleHistoryLog, toggleHistoryCardLog, tailHistoryCardLog, tailHistoryLog, toggleQueueLog, tailQueueLog,
  toggleDashLog, refreshDashLog, tailDashLog } from './components/logpanel.js';
import { logLineHtml, trackLogSse, LOG_SSE_MAX } from './components/logview.js';
import { loadAudit, tqRollbackConfig, tqExportAudit } from './views/audit.js';
import { loadApiTokens, tqCreateToken, tqRevokeToken } from './views/tokens.js';
import { loadSchedules, tqSelectSchedule, tqNewSchedule, tqSchedRun, tqSchedPause, tqSchedResume } from './views/schedule.js';
import { loadAlerts, tqPatchSystemAlert, tqRequestNotifPermission } from './views/alerts.js';
import { loadUsage, setUsageRange, setUsageType, tqToggleUsageSeries } from './views/usage.js';
import { onHistorySearchInput, clearHistorySearch, setHistoryFilter, sortHistory, loadMoreHistory, renderHistory,
  toggleHistoryPageHist, toggleHistoryCardHist, searchAllLogs, openLogSearchRun } from './views/history.js';
import { onQueueSearchInput, _syncQueueSearchClear, _resetQueueSearchForFilter, clearQueueSearch, setQueueFilter, setQueueProject, sortQueue, renderQueue, _renderQueuePagination, goQueuePage, _depMeta, _depTagsHtml, _queueDepHtml, _queueSelKey, _queueCheckHtml, _buildQueueRow, _progressPct, _lastRunProgressHtml, _buildQueueCard, _flushQueueBatch, _flashQueueRow, _reapplyQueueFlash, gotoQueueDep, gotoQueueProject } from './views/queue.js';
import { renderDashboard, _dashExpectedSec, _ensureDashStats, toggleDashHist, renderWorkers } from './views/dashboard.js';
import { loadDeps, renderDag, showDagTip, hideDagTip, dagNodeClick, hideDagCtxMenu, showDagCtxMenu, _dagRefreshAfterAction, _dagResetView, dagCtxStop, dagCtxRetry, dagCtxForce, dagCtxMarkProcessed, dagCtxEnable, tqDagZoomBy, tqDagScaleFrom } from './views/deps.js';
import { loadGroups, groupStop, groupRetry, _populateGroupChips, _updateGroupChips, _toggleGroupBar, setGroupFilter, viewGroupInQueue } from './views/groups.js';
import { loadResources, loadHardware, renderClusterUsage, renderResourceBars, renderHardware, saveResLimits, toggleResPanel } from './views/resources.js';
import { _closeGenModal, openModal, openAlert, openConfirm, openYamlModal, closeYamlModal, yamlModalToggleEdit, yamlModalSave, getResourceModalRunId, _resourceModalBodyHtml, showResourceModal, compareRun, closeResourceModal, yamlModalCopy, showToast, showAnchorToast, showUndoToast } from './components/modals.js';
import { _pageSize, setQueueActive, _captureLogScrollStates, _restoreLogScrollStates, tab, jumpToStatus, _isRestartStatus, _requeueLabel } from './core/nav.js';
import { tqHeaderProject, tqProjectQuery, tqLoadTab, tqLoop, tqSetLang, tqRefreshBusy, tqTimelineBusy, tqMobSearchInput, tqSelectVisibleFrom, tqBusy, tqRefreshActivePage, _showPageEmpty } from './core/loader.js';
import { tqAppendLogText, tqCloseLog, tqClearLogView, tqOpenLogPanel, tqLoadStaticLog, tqSetLogFilter, tqSetLogLevel, tqToggleLogWrap, tqDownloadLog, tqLogFilterRegex } from './components/logdock.js';
import { _trackLogSse, toggleTaskHistory, _loadTaskRunHistoryHtml, _renderTaskRunHistoryHtml, _taskStatsStripHtml, toggleCardLog, toggleCardHistory, _panelKey, _sseAppend, _ansiToHtml, _fetchLog, copyTaskName, copyLogTail, _resolveLogPanel, _ensureLogOpen, openLogIssue } from './components/runlogs.js';
import { _etaRemainingSec, fmtCpu, _runningIdentity, _gaugeStructureKey, _gpuBadgeHtml, _logChipsHtml, _usageHtml, _computeGaugeModel, _patchDashboardLive, _updateRunningTimes } from './components/runmetrics.js';
import { badge, _waitKind, friendlyErrorText, waitReasonHtml, res, _taskCopyIconBtn, _updateSortTh } from './util/labels.js';
import { _populateProjectSelect, _populateWorkerSelect, onProjectChange, onWorkerChange, _populateProjectChips, _updateTagChips, _toggleTagBar, toggleTagFilter, _updateBrokenDepsBanner, dismissBrokenDepsBanner } from './components/filterbars.js';
import { onTasksSearchInput, _syncTasksSearchClear, clearTasksSearch, _renderTaskStatsTable, loadTaskStats } from './views/taskstats.js';
import { openMobTaskPicker, closeMobTaskPicker, renderMobTaskList, mobForceTask } from './components/mobpicker.js';
import { tqQueueActive, tqCloseShortcuts, tqToggleShortcuts, tqHeaderMark, tqSyncHeaderMark, tqQueueRowEls, tqHighlightedTask, tqMoveQueueRow, tqTypingTarget, tqHandleKey } from './components/shortcuts.js';
import { tqPersistSavedFilters, tqLoadSavedFilters, tqFilterLabel, tqRenderSavedFilters, tqToggleSavedFilters, tqSyncStatusChips, tqApplySavedFilter, tqDeleteSavedFilter, tqSaveCurrentFilter, tqClearQueueFilter, tqRenderFilterSummary, tqSyncQueueChrome, tqSyncSelectAll, tqSetRowSelected, tqToggleSelectVisible, tqClearSelection, tqRenderBulkBar, tqBulkAction, tqSelectAllMatching, tqRetryAllFailed, _csvCell, tqExportQueue } from './components/queuefilters.js';
import { openCfg, closeCfg, _postJson, saveCfg, _sendJson, toastResult } from './components/cfgmodal.js';
import { retryTaskNew, copyTaskYaml, startPrioEdit, _btnPending, _btnDone, _optimisticQueueStatus, stopTask, retryTask, forceTask, markProcessed, enableTask, setPriority, _updateFreezeBtn, _showProjectRoot, toggleProjectFreeze, toggleProjectPause, tqRerunRun, stopAllProject, workerSync, tqSetPriority, tqPromptPriority, tqSetEnabled } from './components/taskactions.js';
import { _loadTabData, tqLoadTabBusy, _updateStatusPill, _updatePageTitle, _updateHealth, _setHealthFavicon, _statusScopeProject, _scopeStatusPayload, refresh, _setOffline, _updateSseIndicator, startLiveUpdates, _dataVersionChanged, _queueStructuralKey, _patchQueueLiveFromRunning } from './core/refresh.js';
import { UI_TABS, _lsGet, _readUiUrl, _mergedUiState, _buildUiSearchParams, _syncUrl, _activateTabFromUrl, _applyUiVars, setProject, _syncProjBarChips, _syncProjSelect, setProjectAndWait } from './core/url.js';
import { _sparkEnsureLoaded, _sparkSave, _sparkScale, _sparkUnit, _sparkFmt, _sparkCaption, tqRecordSparks, _sparkPoints, _sparkMarkup, tqDrawSparks, _paintSparkline, _drawSparklineOnCanvas, _drawSparkline } from './components/sparks.js';
import { _tlClock, _tlDay, _tlAt, _tlTicks, loadTimeline, _loadTimelineNow, _renderTimeline } from './views/timeline.js';
import { _activePageTab, _helpUiLang, _fetchHelpPayload, _helpTopicHtml, _renderHelpBody, openHelp, closeHelp, tqOpenGuide, loadDocsPage, _docsScrollToSection, _docsRenderSectionNav, tqDocsJump, loadDoc } from './views/docs.js';
import { tqApplyQueueColumns, tqToggleColumnsMenu, tqSetQueueColumn, tqSetQueueColumnFromBox, tqHiddenQueueCols, tqQueueColumnsCss } from './components/columns.js';
import { tqOpenPalette, tqClosePalette, tqPaletteItems, tqPaletteFilter } from './components/palette.js';
import { tqFindTask, tqOnTaskCellClick, tqOpenDrawer, tqCloseDrawer, tqSyncDrawerLive, tqPaintDrawerHead, tqDrawerPrio, tqDrawerToggleEnabled, tqDrawerTab, tqInfoCard, tqPaintDrawerBody, tqDrawerLoadYaml, tqDrawerToggleYaml, tqDrawerSaveYaml, tqDrawerCopyYaml, tqDrawerLoadHistory, tqDrawerAction, tqDrawerWhy, metricSparkSvg, tqDrawerLoadSeries } from './components/drawer.js';
import { tqApplyTheme, tqApplyFont, togglePrefs, tqSyncPrefs, tqSetQuietHours, tqInQuietHours, tqSyncOperatorPrefs, tqSetOperator, tqSetMineScope } from './components/prefs.js';
import { toggleNotifPanel, _notifMarkRead, tqAlertDetailHtml, tqToggleAlertDetail, _openAlertsTab, _fetchNotifications, _renderNotifPanel, tqSetNotifTypeFilter, tqSetNotifProjectFilter } from './components/notifications.js';

const _exports = {
  metricSparkSvg, tqDrawerLoadSeries,
  tqSetLogFilter, tqSetLogLevel, tqToggleLogWrap, tqDownloadLog, tqLogFilterRegex,
  tqApplyQueueColumns, tqToggleColumnsMenu, tqSetQueueColumn, tqSetQueueColumnFromBox, tqHiddenQueueCols, tqQueueColumnsCss,
  showUndoToast,
  tqOpenPalette, tqClosePalette, tqPaletteItems, tqPaletteFilter,
  loadResources, loadHardware, renderClusterUsage, renderResourceBars, renderHardware, saveResLimits, toggleResPanel,
  _closeGenModal, openModal, openAlert, openConfirm, openYamlModal, closeYamlModal, yamlModalToggleEdit, yamlModalSave, getResourceModalRunId, _resourceModalBodyHtml, showResourceModal, compareRun, closeResourceModal, yamlModalCopy, showToast, showAnchorToast,
  _pageSize, setQueueActive, _captureLogScrollStates, _restoreLogScrollStates, tab, jumpToStatus, _isRestartStatus, _requeueLabel,
  tqHeaderProject, tqProjectQuery, tqLoadTab, tqLoop, tqSetLang, tqRefreshBusy, tqTimelineBusy, tqMobSearchInput, tqSelectVisibleFrom, tqBusy, tqRefreshActivePage, _showPageEmpty,
  tqAppendLogText, tqCloseLog, tqClearLogView, tqOpenLogPanel, tqLoadStaticLog,
  _trackLogSse, toggleTaskHistory, _loadTaskRunHistoryHtml, _renderTaskRunHistoryHtml, _taskStatsStripHtml, toggleCardLog, toggleCardHistory, _panelKey, _sseAppend, _ansiToHtml, _fetchLog, copyTaskName, copyLogTail, _resolveLogPanel, _ensureLogOpen, openLogIssue,
  _etaRemainingSec, fmtCpu, _runningIdentity, _gaugeStructureKey, _gpuBadgeHtml, _logChipsHtml, _usageHtml, _computeGaugeModel, _patchDashboardLive, _updateRunningTimes,
  badge, _waitKind, friendlyErrorText, waitReasonHtml, res, _taskCopyIconBtn, _updateSortTh,
  _populateProjectSelect, _populateWorkerSelect, onProjectChange, onWorkerChange, _populateProjectChips, _updateTagChips, _toggleTagBar, toggleTagFilter, _updateBrokenDepsBanner, dismissBrokenDepsBanner,
  onTasksSearchInput, _syncTasksSearchClear, clearTasksSearch, _renderTaskStatsTable, loadTaskStats,
  openMobTaskPicker, closeMobTaskPicker, renderMobTaskList, mobForceTask,
  tqQueueActive, tqCloseShortcuts, tqToggleShortcuts, tqHeaderMark, tqSyncHeaderMark, tqQueueRowEls, tqHighlightedTask, tqMoveQueueRow, tqTypingTarget, tqHandleKey,
  tqPersistSavedFilters, tqLoadSavedFilters, tqFilterLabel, tqRenderSavedFilters, tqToggleSavedFilters, tqSyncStatusChips, tqApplySavedFilter, tqDeleteSavedFilter, tqSaveCurrentFilter, tqClearQueueFilter, tqRenderFilterSummary, tqSyncQueueChrome, tqSyncSelectAll, tqSetRowSelected, tqToggleSelectVisible, tqClearSelection, tqRenderBulkBar, tqBulkAction, tqSelectAllMatching, tqRetryAllFailed, _csvCell, tqExportQueue,
  openCfg, closeCfg, _postJson, saveCfg, _sendJson, toastResult,
  retryTaskNew, copyTaskYaml, startPrioEdit, _btnPending, _btnDone, _optimisticQueueStatus, stopTask, retryTask, forceTask, markProcessed, enableTask, setPriority, _updateFreezeBtn, _showProjectRoot, toggleProjectFreeze, toggleProjectPause, tqRerunRun, stopAllProject, workerSync, tqSetPriority, tqPromptPriority, tqSetEnabled,
  _loadTabData, tqLoadTabBusy, _updateStatusPill, _updatePageTitle, _updateHealth, _setHealthFavicon, _statusScopeProject, _scopeStatusPayload, refresh, _setOffline, _updateSseIndicator, startLiveUpdates,
  UI_TABS, _lsGet, _readUiUrl, _mergedUiState, _buildUiSearchParams, _syncUrl, _activateTabFromUrl, _applyUiVars, setProject, _syncProjBarChips, _syncProjSelect, setProjectAndWait,
  _sparkEnsureLoaded, _sparkSave, _sparkScale, _sparkUnit, _sparkFmt, _sparkCaption, tqRecordSparks, _sparkPoints, _sparkMarkup, tqDrawSparks, _paintSparkline, _drawSparklineOnCanvas, _drawSparkline,
  _tlClock, _tlDay, _tlAt, _tlTicks, loadTimeline, _loadTimelineNow, _renderTimeline,
  _activePageTab, _helpUiLang, _fetchHelpPayload, _helpTopicHtml, _renderHelpBody, openHelp, closeHelp, tqOpenGuide, loadDocsPage, _docsScrollToSection, _docsRenderSectionNav, tqDocsJump, loadDoc,
  tqFindTask, tqOnTaskCellClick, tqOpenDrawer, tqCloseDrawer, tqSyncDrawerLive, tqPaintDrawerHead, tqDrawerPrio, tqDrawerToggleEnabled, tqDrawerTab, tqInfoCard, tqPaintDrawerBody, tqDrawerLoadYaml, tqDrawerToggleYaml, tqDrawerSaveYaml, tqDrawerCopyYaml, tqDrawerLoadHistory, tqDrawerAction, tqDrawerWhy,
  tqApplyTheme, tqApplyFont, togglePrefs, tqSyncPrefs, tqSetQuietHours, tqInQuietHours, tqSyncOperatorPrefs, tqSetOperator, tqSetMineScope,
  toggleNotifPanel, _notifMarkRead, tqAlertDetailHtml, tqToggleAlertDetail, _openAlertsTab, _fetchNotifications, _renderNotifPanel, tqSetNotifTypeFilter, tqSetNotifProjectFilter,
  loadGroups, groupStop, groupRetry, _populateGroupChips, _updateGroupChips, _toggleGroupBar, setGroupFilter, viewGroupInQueue,
  loadDeps, renderDag, showDagTip, hideDagTip, dagNodeClick, hideDagCtxMenu, showDagCtxMenu, _dagRefreshAfterAction, _dagResetView, dagCtxStop, dagCtxRetry, dagCtxForce, dagCtxMarkProcessed, dagCtxEnable, tqDagZoomBy, tqDagScaleFrom,
  toggleDashLog, refreshDashLog, tailDashLog, renderDashboard, _dashExpectedSec, _ensureDashStats, toggleDashHist, renderWorkers,
  toggleQueueLog, tailQueueLog,
  onQueueSearchInput, _syncQueueSearchClear, _resetQueueSearchForFilter, clearQueueSearch, setQueueFilter, setQueueProject, sortQueue, renderQueue, _renderQueuePagination, goQueuePage, _depMeta, _depTagsHtml, _queueDepHtml, _queueSelKey, _queueCheckHtml, _buildQueueRow, _progressPct, _lastRunProgressHtml, _buildQueueCard, _flushQueueBatch, _flashQueueRow, _reapplyQueueFlash, gotoQueueDep, gotoQueueProject,
  toggleHistoryLog, toggleHistoryCardLog, tailHistoryCardLog, tailHistoryLog,
  onHistorySearchInput, clearHistorySearch, setHistoryFilter, sortHistory, loadMoreHistory, renderHistory,
  toggleHistoryPageHist, toggleHistoryCardHist, searchAllLogs, openLogSearchRun,
  loadUsage, setUsageRange, setUsageType, tqToggleUsageSeries,
  loadAudit, tqRollbackConfig, tqExportAudit,
  loadApiTokens, tqCreateToken, tqRevokeToken,
  loadSchedules, tqSelectSchedule, tqNewSchedule, tqSchedRun, tqSchedPause, tqSchedResume,
  loadAlerts, tqPatchSystemAlert, tqRequestNotifPermission,
  escHtml, jsq, taskKey, qrowId, domIdPart, fmt, dur,
  _appendLog: appendLog, _LOG_CAP_CHARS: LOG_CAP_CHARS,
  tqEventSource, _fetchJson: fetchJson, _fetchJsonEtag: fetchJsonEtag,
  tqPerfEnabled, tqDominantTimingPhase,
  _dataVersionChanged, _queueStructuralKey, _patchQueueLiveFromRunning,
  tqAct: act, TQ_THIS: THIS, TQ_EVENT: EVENT,
  _applyLogSearch: applyLogSearch, searchLog, logSearchNav, openLogStream, logTailText,
  tqLogLineHtml: logLineHtml, trackLogSse, LOG_SSE_MAX,
};
Object.assign(window, _exports); // app.js (classic script) still calls these by global name
registerActions(_exports); // data- delegation resolves from the registry first

// Shared UI state object; app.js binds `const S = window.tqState` at load.
import { S } from './core/state.js';
import { installA11y } from './util/a11y.js';
window.tqState = S;
if (typeof document !== 'undefined') installActions(document);
if (typeof document !== 'undefined') {
  if (document.body) installA11y(document); else document.addEventListener('DOMContentLoaded', () => installA11y(document));
}
