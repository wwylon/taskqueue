// ES-module entry + UI boot (F-207: classic app.js boot folded here).
// Helpers are exposed on window as shims for templates / data-click and for Node tests.
import { escHtml, jsq, taskKey, qrowId, domIdPart, fmt, dur, appendLog, LOG_CAP_CHARS } from './util/format.js';
import { tqEventSource, openStream, fetchJson, fetchJsonEtag, tqPerfEnabled, tqDominantTimingPhase } from './core/api.js';
import { act, installActions, registerActions, THIS, EVENT } from './core/actions.js';
import { applyLogSearch, searchLog, logSearchNav, openLogStream, logTailText,
  toggleHistoryLog, toggleHistoryCardLog, tailHistoryCardLog, tailHistoryLog, toggleQueueLog, tailQueueLog,
  toggleDashLog, refreshDashLog, tailDashLog } from './components/logpanel.js';
import { logLineHtml, classifyLogLine, trackLogSse, LOG_SSE_MAX } from './components/logview.js';
import { loadAudit, tqRollbackConfig, tqExportAudit } from './views/audit.js';
import { loadApiTokens, tqCreateToken, tqRevokeToken } from './views/tokens.js';
import { loadSchedules, tqSelectSchedule, tqNewSchedule, tqSchedRun, tqSchedPause, tqSchedResume } from './views/schedule.js';
import { loadAlerts, tqPatchSystemAlert, tqRequestNotifPermission } from './views/alerts.js';
import { loadUsage, setUsageRange, setUsageType, tqToggleUsageSeries } from './views/usage.js';
import { onHistorySearchInput, clearHistorySearch, setHistoryFilter, sortHistory, loadMoreHistory, loadPrevHistory, renderHistory,
  toggleHistoryPageHist, toggleHistoryCardHist, searchAllLogs, openLogSearchRun } from './views/history.js';
import { onQueueSearchInput, _syncQueueSearchClear, _resetQueueSearchForFilter, clearQueueSearch, setQueueFilter, setQueueProject, sortQueue, renderQueue, _renderQueuePagination, goQueuePage, _depMeta, _depTagsHtml, _queueDepHtml, _queueSelKey, _queueCheckHtml, _buildQueueRow, _progressPct, _lastRunProgressHtml, _buildQueueCard, _flashQueueRow, _reapplyQueueFlash, gotoQueueDep, gotoQueueProject } from './views/queue.js';
import { renderDashboard, _dashExpectedSec, _ensureDashStats, toggleDashHist, renderWorkers } from './views/dashboard.js';
import { _fitsNowHtml, tqRefreshFitsNow } from './components/fitsnow.js';
import { loadDeps, renderDag, showDagTip, hideDagTip, dagNodeClick, hideDagCtxMenu, showDagCtxMenu, _dagRefreshAfterAction, _dagResetView, dagCtxStop, dagCtxRetry, dagCtxForce, dagCtxMarkProcessed, dagCtxEnable, tqDagZoomBy, tqDagScaleFrom } from './views/deps.js';
import { loadGroups, groupStop, groupRetry, _populateGroupChips, _updateGroupChips, _toggleGroupBar, setGroupFilter, viewGroupInQueue } from './views/groups.js';
import { loadResources, loadHardware, renderClusterUsage, renderResourceBars, renderHardware, saveResLimits, toggleResPanel } from './views/resources.js';
import { _closeGenModal, openModal, openAlert, openConfirm, openYamlModal, closeYamlModal, yamlModalToggleEdit, yamlModalSave, yamlModalCheck, getResourceModalRunId, _resourceModalBodyHtml, showResourceModal, compareRun, closeResourceModal, yamlModalCopy, showToast, showAnchorToast, showUndoToast } from './components/modals.js';
import { _pageSize, setQueueActive, _captureLogScrollStates, _restoreLogScrollStates, tab, jumpToStatus, _isRestartStatus, _requeueLabel } from './core/nav.js';
import { tqHeaderProject, tqProjectQuery, tqLoadTab, tqLoop, tqSetLang, tqRefreshBusy, tqTimelineBusy, tqMobSearchInput, tqSelectVisibleFrom, tqBusy, tqRefreshActivePage, _showPageEmpty } from './core/loader.js';
import { tqAppendLogText, tqCloseLog, tqClearLogView, tqOpenLogPanel, tqLoadStaticLog, tqSetLogFilter, tqSetLogLevel, tqToggleLogWrap, tqDownloadLog, tqLogFilterRegex, tqLogJumpFirstError } from './components/logdock.js';
import { _trackLogSse, toggleTaskHistory, _loadTaskRunHistoryHtml, _renderTaskRunHistoryHtml, _taskStatsStripHtml, toggleCardLog, toggleCardHistory, _panelKey, _sseAppend, _ansiToHtml, _fetchLog, copyTaskName, copyLogTail, _resolveLogPanel, _ensureLogOpen, openLogIssue } from './components/runlogs.js';
import { _etaRemainingSec, fmtCpu, _runningIdentity, _gaugeStructureKey, _gpuBadgeHtml, _logChipsHtml, _usageHtml, _computeGaugeModel, _patchDashboardLive, _updateRunningTimes } from './components/runmetrics.js';
import { badge, _waitKind, friendlyErrorText, waitReasonHtml, res, _taskCopyIconBtn, _updateSortTh } from './util/labels.js';
import { _populateProjectSelect, _populateWorkerSelect, onProjectChange, onWorkerChange, _populateProjectChips, _updateTagChips, _toggleTagBar, toggleTagFilter, _updateBrokenDepsBanner, dismissBrokenDepsBanner } from './components/filterbars.js';
import { projectsInSetFilter, _populateSetBar, tqToggleSetNode, setProjectSet, _updateSetBulkBar, tqSetBulk, _renderCfgProjectSets, tqAssignProjectSet, tqRenameProjectSet } from './components/setfilter.js';
import { onTasksSearchInput, _syncTasksSearchClear, clearTasksSearch, _renderTaskStatsTable, loadTaskStats } from './views/taskstats.js';
import { openMobTaskPicker, closeMobTaskPicker, renderMobTaskList, mobForceTask } from './components/mobpicker.js';
import { tqQueueActive, tqCloseShortcuts, tqToggleShortcuts, tqHeaderMark, tqSyncHeaderMark, tqQueueRowEls, tqHighlightedTask, tqMoveQueueRow, tqTypingTarget, tqHandleKey } from './components/shortcuts.js';
import { tqPersistSavedFilters, tqLoadSavedFilters, tqFilterLabel, tqRenderSavedFilters, tqToggleSavedFilters, tqSyncStatusChips, tqApplySavedFilter, tqDeleteSavedFilter, tqSaveCurrentFilter, tqClearQueueFilter, tqRenderFilterSummary, tqSyncQueueChrome, tqSyncSelectAll, tqSetRowSelected, tqToggleSelectVisible, tqClearSelection, tqRenderBulkBar, tqBulkAction, tqSelectAllMatching, tqRetryAllFailed, _csvCell, tqExportQueue } from './components/queuefilters.js';
import { openCfg, closeCfg, _postJson, saveCfg, _sendJson, toastResult, tqCfgFocusSet } from './components/cfgmodal.js';
import { retryTaskNew, copyTaskYaml, startPrioEdit, _btnPending, _btnDone, _optimisticQueueStatus, stopTask, holdTask, unholdTask, retryTask, forceTask, markProcessed, enableTask, setPriority, _updateFreezeBtn, _showProjectRoot, toggleProjectFreeze, toggleProjectPause, tqRerunRun, stopAllProject, workerSync, tqSetPriority, tqPromptPriority, tqSetEnabled } from './components/taskactions.js';
import { _loadTabData, tqLoadTabBusy, _updateStatusPill, _updatePageTitle, _updateHealth, _setHealthFavicon, _statusScopeProject, _scopeStatusPayload, refresh, _setOffline, _updateSseIndicator, startLiveUpdates, _dataVersionChanged, _queueStructuralKey, _patchQueueLiveFromRunning } from './core/refresh.js';
import { UI_TABS, _lsGet, _readUiUrl, _mergedUiState, _buildUiSearchParams, _syncUrl, _activateTabFromUrl, _applyUiVars, setProject, toggleProjectChip, _syncProjBarChips, _syncProjSelect, setProjectAndWait } from './core/url.js';
import { _sparkEnsureLoaded, _sparkSave, _sparkScale, _sparkUnit, _sparkFmt, _sparkCaption, tqClearSparks, tqRecordSparks, _sparkPoints, _sparkMarkup, tqDrawSparks, _paintSparkline, _drawSparklineOnCanvas, _drawSparkline } from './components/sparks.js';
import { _tlClock, _tlDay, _tlAt, _tlTicks, loadTimeline, _loadTimelineNow, _renderTimeline } from './views/timeline.js';
import { _activePageTab, _helpUiLang, _fetchHelpPayload, _helpTopicHtml, _renderHelpBody, openHelp, closeHelp, tqOpenGuide, loadDocsPage, _docsScrollToSection, _docsRenderSectionNav, tqDocsJump, loadDoc } from './views/docs.js';
import { tqApplyQueueColumns, tqToggleColumnsMenu, tqSetQueueColumn, tqSetQueueColumnFromBox, tqHiddenQueueCols, tqQueueColumnsCss } from './components/columns.js';
import { tqOpenPalette, tqClosePalette, tqPaletteItems, tqPaletteFilter } from './components/palette.js';
import { tqFindTask, tqOnTaskCellClick, tqOpenDrawer, tqCloseDrawer, tqSyncDrawerLive, tqPaintDrawerHead, tqDrawerPrio, tqDrawerToggleEnabled, tqDrawerTab, tqInfoCard, tqPaintDrawerBody, tqDrawerLoadYaml, tqDrawerToggleYaml, tqDrawerSaveYaml, tqDrawerCopyYaml, tqDrawerLoadHistory, tqDrawerAction, tqDrawerWhy, metricSparkSvg, tqDrawerLoadSeries, tqDrawerLoadPrevSeries } from './components/drawer.js';
import { tqApplyTheme, tqApplyFont, togglePrefs, tqSyncPrefs, tqSetQuietHours, tqInQuietHours, tqSyncOperatorPrefs, tqSetOperator, tqSetMineScope, tqSyncTokenPrefs, tqSetApiToken } from './components/prefs.js';
import { toggleNotifPanel, _notifMarkRead, tqAlertDetailHtml, tqToggleAlertDetail, _openAlertsTab, _fetchNotifications, _renderNotifPanel, tqSetNotifTypeFilter, tqSetNotifProjectFilter } from './components/notifications.js';

const _exports = {
  metricSparkSvg, tqDrawerLoadSeries, tqDrawerLoadPrevSeries,
  tqSetLogFilter, tqSetLogLevel, tqToggleLogWrap, tqDownloadLog, tqLogFilterRegex,
  tqApplyQueueColumns, tqToggleColumnsMenu, tqSetQueueColumn, tqSetQueueColumnFromBox, tqHiddenQueueCols, tqQueueColumnsCss,
  showUndoToast,
  tqOpenPalette, tqClosePalette, tqPaletteItems, tqPaletteFilter,
  loadResources, loadHardware, renderClusterUsage, renderResourceBars, renderHardware, saveResLimits, toggleResPanel,
  _closeGenModal, openModal, openAlert, openConfirm, openYamlModal, closeYamlModal, yamlModalToggleEdit, yamlModalSave, yamlModalCheck, getResourceModalRunId, _resourceModalBodyHtml, showResourceModal, compareRun, closeResourceModal, yamlModalCopy, showToast, showAnchorToast,
  _pageSize, setQueueActive, _captureLogScrollStates, _restoreLogScrollStates, tab, jumpToStatus, _isRestartStatus, _requeueLabel,
  tqHeaderProject, tqProjectQuery, tqLoadTab, tqLoop, tqSetLang, tqRefreshBusy, tqTimelineBusy, tqMobSearchInput, tqSelectVisibleFrom, tqBusy, tqRefreshActivePage, _showPageEmpty,
  tqAppendLogText, tqCloseLog, tqClearLogView, tqOpenLogPanel, tqLoadStaticLog, tqLogJumpFirstError,
  _trackLogSse, toggleTaskHistory, _loadTaskRunHistoryHtml, _renderTaskRunHistoryHtml, _taskStatsStripHtml, toggleCardLog, toggleCardHistory, _panelKey, _sseAppend, _ansiToHtml, _fetchLog, copyTaskName, copyLogTail, _resolveLogPanel, _ensureLogOpen, openLogIssue,
  _etaRemainingSec, fmtCpu, _runningIdentity, _gaugeStructureKey, _gpuBadgeHtml, _logChipsHtml, _usageHtml, _computeGaugeModel, _patchDashboardLive, _updateRunningTimes,
  badge, _waitKind, friendlyErrorText, waitReasonHtml, res, _taskCopyIconBtn, _updateSortTh,
  _populateProjectSelect, _populateWorkerSelect, onProjectChange, onWorkerChange, _populateProjectChips, _updateTagChips, _toggleTagBar, toggleTagFilter, _updateBrokenDepsBanner, dismissBrokenDepsBanner,
  projectsInSetFilter, _populateSetBar, tqToggleSetNode, setProjectSet, _updateSetBulkBar, tqSetBulk, _renderCfgProjectSets, tqAssignProjectSet, tqRenameProjectSet,
  onTasksSearchInput, _syncTasksSearchClear, clearTasksSearch, _renderTaskStatsTable, loadTaskStats,
  openMobTaskPicker, closeMobTaskPicker, renderMobTaskList, mobForceTask,
  tqQueueActive, tqCloseShortcuts, tqToggleShortcuts, tqHeaderMark, tqSyncHeaderMark, tqQueueRowEls, tqHighlightedTask, tqMoveQueueRow, tqTypingTarget, tqHandleKey,
  tqPersistSavedFilters, tqLoadSavedFilters, tqFilterLabel, tqRenderSavedFilters, tqToggleSavedFilters, tqSyncStatusChips, tqApplySavedFilter, tqDeleteSavedFilter, tqSaveCurrentFilter, tqClearQueueFilter, tqRenderFilterSummary, tqSyncQueueChrome, tqSyncSelectAll, tqSetRowSelected, tqToggleSelectVisible, tqClearSelection, tqRenderBulkBar, tqBulkAction, tqSelectAllMatching, tqRetryAllFailed, _csvCell, tqExportQueue,
  openCfg, closeCfg, _postJson, saveCfg, _sendJson, toastResult, tqCfgFocusSet,
  retryTaskNew, copyTaskYaml, startPrioEdit, _btnPending, _btnDone, _optimisticQueueStatus, stopTask, holdTask, unholdTask, retryTask, forceTask, markProcessed, enableTask, setPriority, _updateFreezeBtn, _showProjectRoot, toggleProjectFreeze, toggleProjectPause, tqRerunRun, stopAllProject, workerSync, tqSetPriority, tqPromptPriority, tqSetEnabled,
  _loadTabData, tqLoadTabBusy, _updateStatusPill, _updatePageTitle, _updateHealth, _setHealthFavicon, _statusScopeProject, _scopeStatusPayload, refresh, _setOffline, _updateSseIndicator, startLiveUpdates,
  UI_TABS, _lsGet, _readUiUrl, _mergedUiState, _buildUiSearchParams, _syncUrl, _activateTabFromUrl, _applyUiVars, setProject, toggleProjectChip, _syncProjBarChips, _syncProjSelect, setProjectAndWait,
  _sparkEnsureLoaded, _sparkSave, _sparkScale, _sparkUnit, _sparkFmt, _sparkCaption, tqClearSparks, tqRecordSparks, _sparkPoints, _sparkMarkup, tqDrawSparks, _paintSparkline, _drawSparklineOnCanvas, _drawSparkline,
  _tlClock, _tlDay, _tlAt, _tlTicks, loadTimeline, _loadTimelineNow, _renderTimeline,
  _activePageTab, _helpUiLang, _fetchHelpPayload, _helpTopicHtml, _renderHelpBody, openHelp, closeHelp, tqOpenGuide, loadDocsPage, _docsScrollToSection, _docsRenderSectionNav, tqDocsJump, loadDoc,
  tqFindTask, tqOnTaskCellClick, tqOpenDrawer, tqCloseDrawer, tqSyncDrawerLive, tqPaintDrawerHead, tqDrawerPrio, tqDrawerToggleEnabled, tqDrawerTab, tqInfoCard, tqPaintDrawerBody, tqDrawerLoadYaml, tqDrawerToggleYaml, tqDrawerSaveYaml, tqDrawerCopyYaml, tqDrawerLoadHistory, tqDrawerAction, tqDrawerWhy,
  tqApplyTheme, tqApplyFont, togglePrefs, tqSyncPrefs, tqSetQuietHours, tqInQuietHours, tqSyncOperatorPrefs, tqSetOperator, tqSetMineScope, tqSyncTokenPrefs, tqSetApiToken,
  toggleNotifPanel, _notifMarkRead, tqAlertDetailHtml, tqToggleAlertDetail, _openAlertsTab, _fetchNotifications, _renderNotifPanel, tqSetNotifTypeFilter, tqSetNotifProjectFilter,
  loadGroups, groupStop, groupRetry, _populateGroupChips, _updateGroupChips, _toggleGroupBar, setGroupFilter, viewGroupInQueue,
  loadDeps, renderDag, showDagTip, hideDagTip, dagNodeClick, hideDagCtxMenu, showDagCtxMenu, _dagRefreshAfterAction, _dagResetView, dagCtxStop, dagCtxRetry, dagCtxForce, dagCtxMarkProcessed, dagCtxEnable, tqDagZoomBy, tqDagScaleFrom,
  toggleDashLog, refreshDashLog, tailDashLog, renderDashboard, _dashExpectedSec, _ensureDashStats, toggleDashHist, renderWorkers,
  _fitsNowHtml, tqRefreshFitsNow,
  toggleQueueLog, tailQueueLog,
  onQueueSearchInput, _syncQueueSearchClear, _resetQueueSearchForFilter, clearQueueSearch, setQueueFilter, setQueueProject, sortQueue, renderQueue, _renderQueuePagination, goQueuePage, _depMeta, _depTagsHtml, _queueDepHtml, _queueSelKey, _queueCheckHtml, _buildQueueRow, _progressPct, _lastRunProgressHtml, _buildQueueCard, _flashQueueRow, _reapplyQueueFlash, gotoQueueDep, gotoQueueProject,
  toggleHistoryLog, toggleHistoryCardLog, tailHistoryCardLog, tailHistoryLog,
  onHistorySearchInput, clearHistorySearch, setHistoryFilter, sortHistory, loadMoreHistory, loadPrevHistory, renderHistory,
  toggleHistoryPageHist, toggleHistoryCardHist, searchAllLogs, openLogSearchRun,
  loadUsage, setUsageRange, setUsageType, tqToggleUsageSeries,
  loadAudit, tqRollbackConfig, tqExportAudit,
  loadApiTokens, tqCreateToken, tqRevokeToken,
  loadSchedules, tqSelectSchedule, tqNewSchedule, tqSchedRun, tqSchedPause, tqSchedResume,
  loadAlerts, tqPatchSystemAlert, tqRequestNotifPermission,
  escHtml, jsq, taskKey, qrowId, domIdPart, fmt, dur,
  _appendLog: appendLog, _LOG_CAP_CHARS: LOG_CAP_CHARS,
  tqEventSource, openStream, _fetchJson: fetchJson, _fetchJsonEtag: fetchJsonEtag,
  tqPerfEnabled, tqDominantTimingPhase,
  _dataVersionChanged, _queueStructuralKey, _patchQueueLiveFromRunning,
  tqAct: act, TQ_THIS: THIS, TQ_EVENT: EVENT,
  _applyLogSearch: applyLogSearch, searchLog, logSearchNav, openLogStream, logTailText,
  tqLogLineHtml: logLineHtml, classifyLogLine, trackLogSse, LOG_SSE_MAX,
};
Object.assign(window, _exports);
registerActions(_exports);

import { S } from './core/state.js';
import { installA11y } from './util/a11y.js';
window.tqState = S;

/** Full UI boot (was classic app.js). Idempotent. */
export function tqBootApp() {
  if (typeof document === 'undefined' || window.__tqBooted) return;
  window.__tqBooted = true;
  installActions(document);
  installA11y(document);

  S.historyLimit = S.historyPageSize;
  S.bootUi = _mergedUiState();
  _applyUiVars(S.bootUi);
  tqSyncPrefs();
  document.addEventListener('keydown', tqHandleKey);

  const name = _activateTabFromUrl((S.bootUi && S.bootUi.tab) || 'queue');
  tqLoadTabBusy(name);
  _syncUrl({ replace: true });

  window.addEventListener('popstate', function () {
    const fromUrl = _readUiUrl();
    S.applyingUrl = true;
    let tabName;
    try {
      _applyUiVars({
        project: fromUrl.project || 'all',
        status: fromUrl.status || 'all',
        q: Object.prototype.hasOwnProperty.call(fromUrl, 'q') ? fromUrl.q : '',
        group: fromUrl.group || null,
        set: fromUrl.set || null,
        tag: fromUrl.tag || null,
        doc: fromUrl.doc || null,
      });
      tabName = _activateTabFromUrl(fromUrl.tab || 'queue');
    } finally {
      S.applyingUrl = false;
    }
    tqLoadTabBusy(tabName);
    _syncUrl({ replace: true });
  });

  startLiveUpdates();

  const _origTab = window.tab;
  window.tab = function (n, btn) {
    _origTab(n, btn);
    if (n === 'usage') loadUsage();
  };

  const _mobPicker = document.getElementById('mob-task-picker');
  if (_mobPicker) {
    _mobPicker.addEventListener('click', function (e) {
      if (e.target === this) closeMobTaskPicker();
    });
  }

  tqLoadSavedFilters();
  tqApplyQueueColumns();
  const _queueRoot = document.getElementById('queue');
  if (_queueRoot) {
    // Shift-click selects a contiguous range of visible rows (anchor = last plain click).
    _queueRoot.addEventListener('click', function (e) {
      const cb = e.target.closest && e.target.closest('.q-row-check');
      if (!cb || !e.shiftKey || !S.queueSelectAnchor) return;
      const rows = S.queueExportRows || S.queue || [];
      const keys = rows.map(t => _queueSelKey(t.project, t.task_id));
      const a = keys.indexOf(S.queueSelectAnchor);
      const cur = _queueSelKey(cb.getAttribute('data-project'), cb.getAttribute('data-task'));
      const b = keys.indexOf(cur);
      if (a < 0 || b < 0) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      const [lo, hi] = a < b ? [a, b] : [b, a];
      for (let i = lo; i <= hi; i++) {
        const k = keys[i];
        const sep = k.indexOf('\0');
        tqSetRowSelected(k.slice(0, sep), k.slice(sep + 1), true);
      }
      document.querySelectorAll('#queue .q-row-check').forEach(el => {
        const k = _queueSelKey(el.getAttribute('data-project'), el.getAttribute('data-task'));
        el.checked = S.queueSelection.has(k);
      });
    }, true);
    _queueRoot.addEventListener('change', function (e) {
      const cb = e.target.closest && e.target.closest('.q-row-check');
      if (!cb) return;
      const project = cb.getAttribute('data-project');
      const task = cb.getAttribute('data-task');
      tqSetRowSelected(project, task, cb.checked);
      S.queueSelectAnchor = _queueSelKey(project, task);
    });
  }
  document.addEventListener('click', function (e) {
    const pop = document.getElementById('saved-filters-pop');
    const btn = document.getElementById('saved-filters-btn');
    if (!pop || pop.hidden) return;
    if (pop.contains(e.target) || (btn && btn.contains(e.target))) return;
    pop.hidden = true;
  });

  (function () {
    let _notifInFlight = false;
    async function _pollNotif() {
      if (_notifInFlight) return;
      _notifInFlight = true;
      try { await _fetchNotifications(false); }
      finally { _notifInFlight = false; }
    }
    window.tqNotifOnLiveEvent = _pollNotif;
    _pollNotif();
    let _notifTimer = null;
    function _scheduleNotifPoll() {
      if (_notifTimer) clearTimeout(_notifTimer);
      const live = typeof S !== 'undefined' && S.sseState === 'live';
      _notifTimer = setTimeout(async () => {
        await _pollNotif();
        _scheduleNotifPoll();
      }, live ? 30000 : 10000);
    }
    _scheduleNotifPoll();
  })();

  (function () {
    if ('serviceWorker' in navigator && location.protocol !== 'file:') {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    }
    function sync() {
      const b = document.getElementById('offline-banner');
      if (!b) return;
      if (navigator.onLine) {
        if (b.dataset.pwaOffline === '1') {
          b.style.display = 'none';
          delete b.dataset.pwaOffline;
        }
        return;
      }
      b.dataset.pwaOffline = '1';
      b.style.display = '';
      const msg = b.querySelector('[data-i18n="status.offline"]') || b;
      if (typeof i18n !== 'undefined') {
        try { msg.textContent = i18n.t('pwa.offline') || i18n.t('status.offline'); } catch (_) {}
      }
    }
    window.addEventListener('online', sync);
    window.addEventListener('offline', sync);
    sync();
  })();
}

window.tqBootApp = tqBootApp;
registerActions({ tqBootApp });

if (typeof document !== 'undefined') {
  const go = () => { try { tqBootApp(); } catch (e) { console.error('[tq] boot failed', e); } };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go, { once: true });
  else go();
}
