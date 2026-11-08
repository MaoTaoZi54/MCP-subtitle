'use strict';

const S = {
  meta: null,
  tools: {},
  config: null,
  page: 'overview',
  timer: null,
  jobTimer: null,
  filter: '',
  activeTab: {},
  pending: null,      // 结果接力：{ tool, args }，由 sendToTool 写入、toolCard 消费
  recentPaths: [],    // 最近用过的路径（localStorage 持久化）
  scrollTo: null,     // 路由渲染完后需要滚动到的元素 id
  configDirty: false, // 配置页有未保存的修改（离开时拦截）
};

const NAV = [
  { id: 'overview', label: '概览', icon: 'overview' },
  { id: 'browse', label: '媒体浏览', icon: 'browse', tools: ['nas_list_directory', 'nas_find_files', 'nas_disk_usage'] },
  { id: 'scan', label: '扫描体检', icon: 'scan', tools: ['nas_scan_media', 'media_quality_analyze', 'media_library_audit', 'duplicate_media_detect', 'media_missing_episodes', 'media_integrity_check', 'nas_check_subtitle', 'nas_classify_region'] },
  { id: 'subtitle', label: '字幕', icon: 'subtitle', tools: ['media_fill_subtitles', 'subhd_search', 'subhd_download', 'media_mux_subtitles'] },
  { id: 'resource', label: '资源搜索', icon: 'resource', tools: ['media_search', 'jackett_search', 'seedhub_search', 'seedhub_pans', 'seedhub_magnets', 'dysbk_search', 'dysbk_links', 'resolve_media'] },
  { id: 'organize', label: '整理清理', icon: 'organize', tools: ['organize_media', 'find_media', 'media_cleanup', 'media_rollback'] },
  { id: 'config', label: '配置', icon: 'config' },
  { id: 'workbench', label: '工具台', icon: 'workbench' },
];

// 工具台按用途分组：覆盖全部 28 个工具，分组可折叠，展开状态记在本地
const WORKBENCH_GROUPS = [
  {
    id: 'io', title: '目录与磁盘',
    hint: '翻看文件夹、找文件、看空间与当前配置',
    tools: ['nas_list_directory', 'nas_find_files', 'nas_disk_usage', 'nas_get_config'],
  },
  {
    id: 'scan', title: '媒体扫描与体检',
    hint: '扫描媒体库、查画质/重复/缺集/损坏',
    tools: ['nas_scan_media', 'nas_check_subtitle', 'nas_classify_region', 'media_quality_analyze',
      'duplicate_media_detect', 'media_library_audit', 'media_missing_episodes', 'media_integrity_check'],
  },
  {
    id: 'subtitle', title: '字幕',
    hint: '搜索、下载、批量补齐与混流字幕',
    tools: ['subhd_search', 'subhd_download', 'media_fill_subtitles', 'media_mux_subtitles'],
  },
  {
    id: 'resource', title: '资源搜索',
    hint: '聚合搜索、各站点搜索与网盘/磁力解析',
    tools: ['media_search', 'jackett_search', 'seedhub_search', 'seedhub_magnets', 'seedhub_pans',
      'dysbk_search', 'dysbk_links', 'resolve_media'],
  },
  {
    id: 'cleanup', title: '整理与清理',
    hint: '规范整理、定位影片、垃圾清理与回滚',
    tools: ['organize_media', 'find_media', 'media_cleanup', 'media_rollback'],
  },
];

const WORKBENCH_OPEN_KEY = 'nas-workbench-groups';

function loadWorkbenchOpen() {
  try {
    const raw = JSON.parse(localStorage.getItem(WORKBENCH_OPEN_KEY) || '{}');
    return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  } catch (_) {
    return {};
  }
}

function saveWorkbenchOpen(state) {
  try { localStorage.setItem(WORKBENCH_OPEN_KEY, JSON.stringify(state)); } catch (_) { /* 隐私模式忽略 */ }
}

const LABELS = {
  name: '名称', title: '标题', path: '路径', relative_path: '相对路径', type: '类型',
  media_category: '类别', status: '状态', season: '季', episode: '集', resolution: '分辨率',
  codec: '编码', hdr: 'HDR', audio: '音轨', chinese_sub: '中字', score: '评分', site: '站点',
  format: '格式', languages: '语言', release_name: '发布名', pack_hint: '整季包', size: '大小',
  source: '来源', seeders: '做种', confidence: '置信度', verified: '已验证',
  mismatch: '标题不匹配', warning: '提示', magnet: '磁力链接', pan_url: '网盘链接',
  quality_tag: '画质标记', updated: '更新时间', real_files: '文件数',
  size_bytes: '大小', mtime: '修改时间', duration: '时长', duration_seconds: '时长',
  best_path: '建议保留', reason: '原因', message: '说明', mount_point: '挂载点', fs: '文件系统',
  total_human: '总量', used_human: '已用', free_human: '可用', percent_used: '使用率',
  id: 'ID', tool: '工具', created_at: '创建时间', finished_at: '完成时间', duration_ms: '耗时',
  season_count: '季数', episode_count: '集数', file_count: '文件数', group_count: '组数',
  max_list_entries: '列表条数上限', max_search_results: '搜索结果上限', max_scan_files: '扫描文件数上限',
  max_list_depth: '目录层数上限', max_errors_reported: '错误条目上限',
  indexer: '索引器', year: '年份', media_type: '媒体类型', canonical_title: '规范片名',
  original_title: '原始片名', original_language: '原始语言', origin_country: '出品地区',
  tmdb_id: 'TMDB 编号', tvdb_id: 'TVDB 编号', imdb_id: 'IMDb 编号', douban_id: '豆瓣编号',
  page_url: '详情页', detail_url: '详情页', share_url: '分享页', file_id: '文件编号',
  share_id: '分享编号', host: '网盘', url: '链接', link: '链接', links: '链接',
  quality: '画质', edition: '版本', group: '分组', tag: '标签', video_codec: '视频编码',
  audio_codec: '音频编码', bit_rate: '码率', frame_rate: '帧率', source_type: '来源类型',
  is_chinese: '是否国产', has_chinese_subtitle: '有中文字幕', missing_episodes: '缺少的集',
  detected: '识别结果', expected: '预期', actual: '实际', matches: '匹配情况', query: '搜索词',
  total_matched: '匹配总数', returned: '本次返回', truncated: '结果被截断', offset: '跳过条数',
  limit: '返回上限', recursive: '包含子目录', roots: '搜索位置', extension: '扩展名',
  extensions: '扩展名', free_bytes: '剩余字节', used_bytes: '已用字节', total_bytes: '总字节',
  fstype: '磁盘格式', ok: '是否成功', error: '错误', code: '错误码', level: '级别',
  count: '数量', size_human: '大小', source_path: '源路径', target_path: '目标路径',
  device: '设备', mounted: '挂载点', sampled_from: '采样来源', scope: '范围',
  skipped: '已跳过', missing: '缺失项', root: '根目录', mount: '挂载点',
};

const PRIORITY = ['name', 'title', 'relative_path', 'path', 'status', 'media_category', 'type',
  'season', 'episode', 'resolution', 'codec', 'hdr', 'audio', 'chinese_sub', 'score', 'site',
  'format', 'languages', 'release_name', 'pack_hint', 'size', 'mtime', 'duration', 'best_path',
  'reason', 'mount_point', 'total_human', 'free_human', 'percent_used', 'tool', 'id',
  'created_at', 'finished_at', 'duration_ms', 'message'];

// ---------------------------------------------------------------- 枚举参数
// 后端取值固定或有限可枚举的参数，一律做成下拉/多选，避免手输拼错。
const LANG_OPTS = [
  ['', '不限 / 自动'], ['zh-Hans', '简体中文'], ['zh-Hant', '繁体中文'], ['zh', '中文（不分简繁）'],
  ['en', '英语'], ['ja', '日语'], ['ko', '韩语'],
];
const SUB_EXT_OPTS = [['srt', 'SRT'], ['ass', 'ASS'], ['ssa', 'SSA'], ['sub', 'SUB'], ['sup', 'SUP'], ['vtt', 'VTT']];
const MEDIA_TYPE_FULL = [
  ['', '不限 / 自动判断'], ['movie', '电影'], ['tv', '剧集'],
  ['anime', '动画'], ['documentary', '纪录片'], ['variety', '综艺'], ['unknown', '未识别'],
];
const MEDIA_TYPE_SEARCH = [['', '自动判断'], ['movie', '电影'], ['tv', '剧集']];

// 按工具覆盖（同名参数在不同工具里取值域不同）
const TOOL_ENUMS = {
  nas_scan_media: { media_type: MEDIA_TYPE_FULL },
  jackett_search: { media_type: MEDIA_TYPE_SEARCH },
  resolve_media: { media_type: MEDIA_TYPE_SEARCH },
  media_integrity_check: { mode: [['basic', '快速检查'], ['deep', '深度检查']] },
  organize_media: {
    mode: [['all', '全部文件'], ['movie', '只处理电影'], ['tv', '只处理剧集']],
    preexisting_empty_scope: [['library_root', '媒体库根目录范围'], ['target_subtree', '目标目录范围']],
  },
  nas_find_files: { category: [['', '不限（全部文件）'], ['video', '只视频'], ['subtitle', '只字幕']] },
  subhd_download: { lang: LANG_OPTS, ext: SUB_EXT_OPTS },
  media_fill_subtitles: { lang: LANG_OPTS, ext: SUB_EXT_OPTS },
};

// 可多选枚举：值为数组的参数，给出勾选条，提交时拼成列表
const MULTI_ENUM = {
  // extensions：按配置里的字幕/视频扩展名勾选
  extensions: () => {
    const cfg = S.config || {};
    const seen = new Set();
    const out = [];
    for (const e of cfg.subtitle_extensions || []) {
      const v = String(e).replace(/^\./, '');
      if (v && !seen.has(v)) { seen.add(v); out.push([v, t(`字幕 .${v}`, `字幕 .${v}`)]); }
    }
    for (const e of cfg.video_extensions || []) {
      const v = String(e).replace(/^\./, '');
      if (v && !seen.has(v)) { seen.add(v); out.push([v, t(`视频 .${v}`, `视频 .${v}`)]); }
    }
    return out.length ? out : SUB_EXT_OPTS.concat([['mkv', 'MKV'], ['mp4', 'MP4']]);
  },
};

function enumOptions(tool, name) {
  const perTool = TOOL_ENUMS[tool];
  if (perTool && perTool[name]) return perTool[name];
  return OPTION_SETS[name] || null;
}

// ---------------------------------------------------------------- 友好版文案
// 现有硬编码字符串即专业版文案；友好版通过 t() 与下面的映射表换词，专业版零改动。
const FRIENDLY = {
  tool: {
    nas_get_config: '看看当前设置',
    nas_list_directory: '翻看文件夹',
    nas_find_files: '按名字找文件',
    nas_disk_usage: '看磁盘还剩多少空间',
    nas_scan_media: '给媒体文件做体检',
    nas_check_subtitle: '查查字幕信息',
    nas_classify_region: '判断是不是国产剧',
    media_quality_analyze: '检查画质和音轨',
    duplicate_media_detect: '找出重复的文件',
    media_library_audit: '盘点整个媒体库',
    media_missing_episodes: '找出缺的剧集',
    media_integrity_check: '检查文件有没有损坏',
    media_cleanup: '清理垃圾文件',
    media_rollback: '撤销上次的整理',
    subhd_search: '在 SubHD 找字幕',
    subhd_download: '下载 SubHD 字幕',
    media_fill_subtitles: '批量补字幕',
    media_mux_subtitles: '把字幕合并进视频文件',
    resolve_media: '从文件名认出是哪部剧',
    jackett_search: '用 Jackett 找资源',
    organize_media: '自动归类文件',
    find_media: '查影片放在哪',
    seedhub_search: '在 SeedHub 搜资源',
    seedhub_magnets: '拿 SeedHub 磁力链接',
    seedhub_pans: '拿 SeedHub 网盘链接',
    dysbk_search: '在网盘分享站搜资源',
    dysbk_links: '拿网盘直链',
    media_search: '一次搜遍所有资源站',
  },
  // 卡片的一句话说明：只讲「这件事帮你干什么」，技术细节留给「技术信息」折叠
  desc: {
    nas_get_config: '查看当前生效的设置项、开关与路径',
    nas_list_directory: '翻看一个文件夹里有哪些文件',
    nas_find_files: '按文件名或后缀在媒体库里找文件',
    nas_disk_usage: '看看磁盘还有多少可用空间',
    nas_scan_media: '扫一遍媒体库，列出影片和剧集',
    nas_check_subtitle: '查一个视频有没有字幕、是什么语言',
    nas_classify_region: '根据音轨和命名判断是不是国产片',
    media_quality_analyze: '检查分辨率、编码、音轨和 HDR 情况',
    duplicate_media_detect: '找出内容重复或疑似重复的多个版本',
    media_library_audit: '整体盘点媒体库，报告问题与统计',
    media_missing_episodes: '检查剧集缺了哪几集',
    media_integrity_check: '检查视频文件有没有损坏',
    media_cleanup: '找出垃圾文件和空目录（默认只预览）',
    media_rollback: '撤销上一次整理或改名操作',
    subhd_search: '在 SubHD 搜索匹配的字幕',
    subhd_download: '下载选中的字幕文件到本地',
    media_fill_subtitles: '批量给缺字幕的剧集补字幕',
    media_mux_subtitles: '把外挂字幕混流进视频文件',
    resolve_media: '从文件名解析片名、年份和季集',
    jackett_search: '通过 Jackett 搜索 PT 资源',
    seedhub_search: '在 SeedHub 搜索影视资源页',
    seedhub_magnets: '从 SeedHub 页面提取磁力链接',
    seedhub_pans: '从 SeedHub 页面提取网盘链接',
    dysbk_search: '在网盘分享站按片名搜资源',
    dysbk_links: '把网盘分享页解析成直链',
    media_search: '一次查询聚合多个资源站的结果',
    organize_media: '按影片信息规范命名并移动文件',
    find_media: '查找某部影片或剧集放在哪个文件夹',
  },
  param: {
    path: '文件夹位置', paths: '视频文件列表', root: '从哪个文件夹开始', roots: '文件夹列表',
    keyword: '关键词', keywords: '关键词（可多个）', query: '搜索词', queries: '搜索词（可多个）',
    title: '标题', name: '名称', names: '名称（可多个）', ext: '文件类型', extensions: '文件类型（每行一个）',
    limit: '最多返回几条', offset: '跳过前几条', season: '第几季', episode: '第几集',
    dry_run: '先看计划，不动文件', source: '来源文件夹', target: '目标文件夹',
    operation_id: '操作编号', min_size: '小于这个体积不算重复', days: '天数',
    language: '语言', lang: '语言', urls: '链接（每行一个）', url: '网址', file: '文件', files: '文件（每行一个）',
    recursive: '连子文件夹一起', category: '类别', mode: '方式', site: '站点', force: '强制执行',
    probe: '读一下视频编码信息', missing_subtitle_only: '只看缺字幕的',
    delete_external: '删掉外挂字幕', replace_original: '直接替换原文件', overwrite: '覆盖已有文件',
    language_split: '按国语/外语分子目录',
  },
  err: {
    write_disabled: '还没有开启「允许改动文件」。到「配置」页打开对应的开关再试。',
    access_denied: '这个文件夹不在允许访问的列表里。可以到「配置」页把它加进去。',
    not_found: '找不到这个文件或文件夹，检查一下路径对不对。',
    invalid_arguments: '填写的内容有点问题，检查一遍再试试。',
    not_configured: '这个功能还没配置好，到「配置」页把对应的密钥或地址填上。',
    unauthorized: '访问密码不对或已过期，请重新登录。',
    timeout: '等了太久还没结果。稍后到「工具台 → 后台任务」里看结果。',
    request_failed: '请求没有发出去，检查网络后再试一次。',
    load_failed: '页面内容加载失败，点右上角「刷新」试试。',
    save_failed: '保存没有成功，请再试一次。',
    too_large: '内容太大了，精简一点再试。',
    bad_response: '服务器返回了看不懂的内容。',
    csrf: '页面太久没动了，刷新一下再试。',
    // —— 以下对应后端实际抛出的错误码（toolkit.py / server.py / jobs.py / nas_mcp/*）
    interrupted: '应用重启时任务被打断了，请重新执行一次。',
    internal_error: '服务内部出了点问题，到「工具台 → 日志」查看详情。',
    unknown_tool: '没有这个功能，刷新页面让菜单同步一下。',
    config_error: '配置文件有问题，检查后再保存。',
    io_error: '读写文件时出错了，检查磁盘与权限。',
    invalid_json: '提交的内容格式不对，检查一遍再试。',
    method_not_allowed: '这个请求方式不被允许。',
    ui_missing: '界面文件缺失，需要重新部署应用。',
    already_exists: '目标位置已经有同名文件了。',
    permission_denied: '系统权限不够，访问不了这个位置。',
    all_sites_failed: '所有字幕站点都失败了，稍后再试或检查站点配置。',
    resolver_error: '没能从文件名认出是哪部剧，可以手动搜索。',
    auth_failed: '站点账号登录失败，到「配置」页检查账号密码。',
    invalid_file_id: '站点返回的文件编号不合法，重新搜索再选一次。',
    site_disabled: '这个站点还没开启，到「配置」页打开它。',
    invalid_argument: '填写的内容有点问题，检查一遍再试试。',
    invalid_sid: '字幕条目编号不对，重新搜索再选一次。',
    detail_empty: '站点没有返回字幕详情，换个条目试试。',
    detail_no_url: '站点没有给出下载链接，稍后再试。',
    download_not_archive: '下载到的不是字幕文件，可能需要登录或被站点限流。',
    subhd_error: 'SubHD 站点出错了，稍后再试。',
    JACKETT_HTTP: 'Jackett 请求失败，检查 Jackett 地址与服务状态。',
    JACKETT_PARSE: 'Jackett 返回的内容解析失败。',
    JACKETT_EMPTY: 'Jackett 没有返回结果。',
  },
  job: {
    queued: '排队中', running: '进行中', succeeded: '完成', failed: '失败',
    canceled: '已取消', interrupted: '已中断',
  },
  risk: { read: '只查看', write: '会修改文件', delete: '会删除文件' },
  category: { 配置: '设置', 文件: '文件', 媒体: '媒体', 运维: '维护', 字幕: '字幕', 资源: '资源', 整理: '整理' },
  field: {
    sampled_from: '检查过的位置', has_chinese_subtitle: '有中文字幕', profile: '规格',
    channels: '声道', fstype: '磁盘格式', total_bytes: '总容量', used_bytes: '已用',
    free_bytes: '剩余', job_id: '任务编号', allowed_paths_expanded: '实际生效的文件夹',
    media_roots_expanded: '媒体库位置', video_extensions: '视频类型', subtitle_extensions: '字幕类型',
    videos: '视频文件', candidates: '搜索结果', entries: '文件夹内容', filesystems: '磁盘',
    jobs: '任务', media: '媒体信息',
    total: '总数', reported: '已列出', errors: '打不开的位置',
    permission_denied: '没有权限的位置', new_permission_denied: '新发现的无权限位置',
    known_permission_denied_suppressed: '已知无权限（重复的不再报）', over_limit_suppressed: '太多没列出的',
    result_incomplete: '结果可能不全', denied_paths_sample: '没权限的位置举例',
    roots: '搜索位置', keyword: '关键词', category: '类别', extensions: '文件类型',
    recursive: '连子文件夹一起', limit: '最多返回几条', offset: '跳过前几条',
    total_matched: '找到的文件数', returned: '本次返回条数', truncated: '还有更多没显示',
    scanned_files: '检查过的文件数', skipped_by_filter: '被跳过的文件数', scan_truncated: '扫描提前结束',
    max_list_entries: '一次最多列出多少条', max_search_results: '最多搜出多少条结果',
    max_scan_files: '最多检查多少个文件', max_list_depth: '往子文件夹里翻几层',
    max_errors_reported: '最多报告多少个错误',
    probe: '读取编码信息', missing_subtitle_only: '只看缺字幕的', media_type: '媒体类型',
    probe_attempts: '探测次数', probed_files: '已探测文件数', probe_budget: '探测上限',
    probe_budget_exhausted: '探测预算用完', total_files: '文件总数', duplicate_groups: '重复组数',
    duplicate_files: '重复文件数', suspect_groups: '可疑组数', tmdb_enabled: 'TMDB 已启用',
    title_resolution: '标题识别方式', pagination: '分页', total_shows: '剧集总数',
    next_offset: '下一页起点', dry_run: '仅预览（不动文件）', mode: '模式',
    enable_write: '允许改动文件', season_pack_threshold: '整季包阈值',
    scanned_count: '已扫描', skipped_count: '已跳过', matched_count: '已匹配',
    budget_seconds: '耗时预算', budget_exhausted: '预算用完', rollback_supported: '支持回滚',
    rollback_note: '回滚说明', search_status: '搜索状态', site_attempts: '站点尝试',
    tried_sites: '尝试过的站点', site_errors: '站点错误', checks: '检查项',
    operation_id: '操作编号', media_root: '媒体库位置', status: '状态', reason: '原因',
    overall: '总体状态', files: '文件', results: '结果', candidates: '候选',
    movie: '电影', tv: '剧集', anime: '动画', documentary: '纪录片', variety: '综艺',
  },
};

const IS_PRO = () => document.documentElement.dataset.uiMode === 'pro';

// ---------------------------------------------------------------- 枚举参数下拉
// 这些参数取值固定或常见，用下拉框替代手输；选「其他…」可回退为手动输入。
const OPTION_SETS = {
  category: [
    ['', '不限（全部文件）'], ['video', '视频文件'], ['subtitle', '字幕文件'],
  ],
  lang: [
    ['zh-Hans', '简体中文'], ['zh-Hant', '繁体中文'], ['zh', '中文（不分简繁）'],
    ['en', '英语'], ['ja', '日语'], ['ko', '韩语'],
  ],
  ext: [
    ['srt', 'SRT 字幕（最常见）'], ['ass', 'ASS 字幕（带特效）'], ['ssa', 'SSA 字幕'],
    ['sub', 'SUB 字幕'], ['sup', 'SUP 字幕（蓝光）'],
  ],
};

function t(proText, friendlyText) {
  return IS_PRO() || friendlyText == null ? proText : friendlyText;
}

// 写操作门（与后端 toolkit.py 里各工具的 enable_* 门控一一对应）：
// 开关关闭时后端会拒绝执行类调用，这里提前在工具卡上提示，不让用户提交后才知道。
const TOOL_GATES = {
  subhd_download: ['enable_write'],
  media_fill_subtitles: ['enable_write'],
  media_mux_subtitles: ['enable_write'],
  organize_media: ['enable_organize', 'enable_delete'],
  media_rollback: ['enable_organize', 'enable_delete'],
  media_cleanup: ['enable_delete'],
};
const GATE_LABELS = {
  enable_write: '允许改动文件',
  enable_organize: '允许自动归类',
  enable_delete: '允许删除文件',
};

// ---------------------------------------------------------------- 媒体识别与路径辅助
function isVideoFile(pathOrName) {
  const s = String(pathOrName || '').toLowerCase();
  return /\.(mkv|mp4|avi|mov|wmv|iso|ts|flv|m4v|webm)$/i.test(s);
}

function cleanMediaTitle(filename) {
  if (!filename) return '';
  let name = String(filename).replace(/\.[a-z0-9]+$/i, '');
  name = name.replace(/\[[^\]]+\]|\([^\)]+\)/g, ' ');
  name = name.replace(/\b(2160p|1080p|720p|4k|uhd|hdr|dv|dovi|remux|web-dl|webrip|bluray|x264|x265|hevc|aac|dts|flac|h264|h265|truehd|atmos)\b/gi, ' ');
  name = name.replace(/\b(s\d{1,2}e\d{1,2}|ep?\d{1,3})\b/gi, ' ');
  name = name.replace(/[\._\-+]/g, ' ');
  return name.trim().replace(/\s+/g, ' ');
}

// ---------------------------------------------------------------- 全局通用文件/目录选择器
function openFilePicker(opts = {}) {
  const { initialPath = '', mode = 'any', onSelect = () => {} } = opts;
  const overlay = el('div', 'modal-overlay');
  const box = el('div', 'modal-box');

  const titleText = mode === 'dir'
    ? t('选择文件夹', '选择文件夹')
    : (mode === 'file' ? t('选择文件', '选择文件') : t('选择路径', '选择路径'));

  const head = el('div', 'modal-head');
  head.appendChild(el('h2')).appendChild(iconLabel('folder', titleText));
  const closeBtn = el('button', 'modal-close');
  closeBtn.appendChild(icon('close'));
  closeBtn.type = 'button';
  closeBtn.title = t('关闭 (Esc)', '关闭');
  closeBtn.setAttribute('aria-label', t('关闭', '关闭'));
  closeBtn.onclick = () => close();
  head.appendChild(closeBtn);
  box.appendChild(head);

  const body = el('div', 'modal-body');

  const crumbBar = el('div', 'file-picker-crumbs');
  body.appendChild(crumbBar);

  const filterInput = document.createElement('input');
  filterInput.type = 'text';
  filterInput.placeholder = t('快速筛选当前目录名称…', '快速筛选当前文件夹…');
  body.appendChild(filterInput);

  const listHolder = el('div', 'file-picker-list');
  body.appendChild(listHolder);
  box.appendChild(body);

  const foot = el('div', 'modal-foot');
  const pathLabel = el('span', 'muted mono', '');
  pathLabel.style.marginRight = 'auto';
  pathLabel.style.overflow = 'hidden';
  pathLabel.style.textOverflow = 'ellipsis';
  pathLabel.style.whiteSpace = 'nowrap';
  pathLabel.style.maxWidth = '360px';
  foot.appendChild(pathLabel);

  const cancelBtn = el('button', 'ghost', t('取消', '取消'));
  cancelBtn.type = 'button';
  cancelBtn.onclick = () => close();
  foot.appendChild(cancelBtn);

  const confirmBtn = el('button', 'primary', t('确定选择', '确定选择'));
  confirmBtn.type = 'button';
  foot.appendChild(confirmBtn);
  box.appendChild(foot);

  overlay.appendChild(box);
  document.body.appendChild(overlay);

  const releaseTrap = modalA11y(overlay, box, titleText);
  function close() {
    window.removeEventListener('keydown', handleKey);
    releaseTrap();
    overlay.remove();
  }

  let curDir = initialPath ? clampToRoots(parentPath(initialPath) || initialPath) : firstAllowedPath();
  let selectedItemPath = (mode === 'dir') ? curDir : initialPath;
  let cachedEntries = [];

  const updatePathLabel = () => {
    pathLabel.textContent = selectedItemPath || curDir;
  };

  const renderCrumbs = () => {
    crumbBar.textContent = '';
    const root = rootOf(curDir) || curDir;
    const parts = curDir.split('/').filter(Boolean);
    let accum = '';
    const roots = allowedRoots();

    if (roots.length > 1) {
      const select = document.createElement('select');
      select.style.fontSize = '12px';
      select.style.padding = '2px 4px';
      roots.forEach((r) => {
        const opt = document.createElement('option');
        opt.value = r;
        opt.textContent = r;
        if (rootOf(curDir) === r) opt.selected = true;
        select.appendChild(opt);
      });
      select.onchange = () => loadDir(select.value);
      crumbBar.appendChild(select);
    }

    parts.forEach((part, i) => {
      accum += '/' + part;
      const thisPath = accum;
      const isLast = (i === parts.length - 1);
      const span = el('span', isLast ? 'file-picker-crumb current' : 'file-picker-crumb', part);
      if (!isLast) {
        span.onclick = () => loadDir(thisPath);
      }
      crumbBar.appendChild(span);
      if (!isLast) crumbBar.appendChild(document.createTextNode(' / '));
    });
  };

  const loadDir = async (dirPath) => {
    curDir = clampToRoots(dirPath);
    if (mode === 'dir') selectedItemPath = curDir;
    updatePathLabel();
    renderCrumbs();
    listHolder.textContent = '';
    listHolder.appendChild(spinner(t('读取目录中…', '正在加载…')));
    try {
      const res = await api('/api/tools/nas_list_directory', { method: 'POST', body: { args: { path: curDir } } });
      cachedEntries = (res && Array.isArray(res.entries)) ? res.entries : [];
      drawEntries();
    } catch (err) {
      listHolder.textContent = '';
      listHolder.appendChild(errorBox(err));
    }
  };

  const drawEntries = () => {
    listHolder.textContent = '';
    const q = filterInput.value.trim().toLowerCase();
    const list = q ? cachedEntries.filter((e) => String(e.name || '').toLowerCase().includes(q)) : cachedEntries;

    const root = rootOf(curDir) || curDir;
    if (curDir !== root) {
      const upItem = el('div', 'file-picker-item');
      upItem.appendChild(el('div', 'item-main')).appendChild(iconLabel('parent', '.. (上一级)'));
      upItem.onclick = () => {
        const parent = curDir.replace(/\/[^/]+$/, '') || root;
        loadDir(parent);
      };
      listHolder.appendChild(upItem);
    }

    if (!list.length) {
      const empty = el('div', 'muted', t('此目录为空', '这里没有文件'));
      empty.style.padding = '16px';
      listHolder.appendChild(empty);
      return;
    }

    const sorted = list.slice().sort((a, b) => {
      const aDir = isDirEntry(a);
      const bDir = isDirEntry(b);
      if (aDir !== bDir) return aDir ? -1 : 1;
      return String(a.name || '').localeCompare(String(b.name || ''), 'zh');
    });

    sorted.forEach((entry) => {
      const isDir = isDirEntry(entry);
      const isVid = !isDir && isVideoFile(entry.name);
      const item = el('div', 'file-picker-item' + (entry.path === selectedItemPath ? ' selected' : ''));

      const main = el('div', 'item-main');
      const iconName = isDir ? 'folder' : (isVid ? 'film' : 'file');
      main.appendChild(iconLabel(iconName, entry.name));
      item.appendChild(main);

      const meta = el('div', 'item-meta');
      if (!isDir && entry.size) meta.textContent = fmtSize(entry.size);
      item.appendChild(meta);

      item.onclick = (e) => {
        e.stopPropagation();
        if (isDir) {
          loadDir(entry.path);
        } else {
          selectedItemPath = entry.path;
          updatePathLabel();
          drawEntries();
        }
      };

      listHolder.appendChild(item);
    });
  };

  filterInput.oninput = drawEntries;

  confirmBtn.onclick = () => {
    const finalPath = selectedItemPath || curDir;
    onSelect(finalPath);
    close();
  };

  const handleKey = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  };
  window.addEventListener('keydown', handleKey);

  overlay.onclick = (e) => {
    if (e.target === overlay) close();
  };

  loadDir(curDir);
}

// ---------------------------------------------------------------- 媒体文件快捷控制台
function openMediaActionModal(entry) {
  if (!entry || !entry.path) return;
  const overlay = el('div', 'modal-overlay');
  const box = el('div', 'modal-box wide');

  const head = el('div', 'modal-head');
  const titleWrap = el('div');
  titleWrap.appendChild(el('h2')).appendChild(iconLabel('film', entry.name || entry.path.split('/').pop()));
  const pathRow = el('div', 'media-quick-path', entry.path);
  titleWrap.appendChild(pathRow);
  head.appendChild(titleWrap);

  const closeBtn = el('button', 'modal-close');
  closeBtn.appendChild(icon('close'));
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', t('关闭', '关闭'));
  closeBtn.onclick = () => close();
  head.appendChild(closeBtn);
  box.appendChild(head);

  const body = el('div', 'modal-body');

  // 写门关闭时先把话说在前面，而不是等用户点了按钮才在最后弹一个 toast
  const writeGateOff = S.config && S.config.enable_write === false;
  if (writeGateOff) {
    const notice = el('div', 'media-gate-notice');
    notice.appendChild(el('span', null, t(
      '当前未开启「允许改动文件」：下载字幕、混流、整理都只会预览，不会真正改动文件。',
      '现在还没开「允许改动文件」，下载字幕、合并、整理都只会预览，不会真的动文件。',
    )));
    const go = el('button', 'ghost small', t('去配置页开启', '去开启'));
    go.type = 'button';
    go.onclick = () => { close(); location.hash = '#/config'; };
    notice.appendChild(go);
    body.appendChild(notice);
  }

  // Tab 按钮
  const nav = el('div', 'media-quick-nav');
  const tabs = [
    { id: 'sub', icon: 'subtitle', label: t('字幕管理与混流', '字幕') },
    { id: 'organize', icon: 'folder', label: t('规范命名与整理', '整理') },
    { id: 'quality', icon: 'search', label: t('画质与体检', '体检') },
  ];
  let activeTab = 'sub';
  const stage = el('div', 'media-quick-body');

  const switchTab = (tabId) => {
    activeTab = tabId;
    nav.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === tabId);
    });
    stage.textContent = '';
    if (tabId === 'sub') renderSubTab();
    else if (tabId === 'organize') renderOrganizeTab();
    else if (tabId === 'quality') renderQualityTab();
  };

  tabs.forEach((tinfo) => {
    const btn = el('button', 'media-quick-tab-btn' + (tinfo.id === activeTab ? ' active' : ''));
    btn.appendChild(iconLabel(tinfo.icon, tinfo.label));
    btn.type = 'button';
    btn.dataset.tab = tinfo.id;
    btn.onclick = () => switchTab(tinfo.id);
    nav.appendChild(btn);
  });
  body.appendChild(nav);
  body.appendChild(stage);
  box.appendChild(body);

  // 渲染【字幕管理】
  const renderSubTab = () => {
    stage.textContent = '';
    // 区块 1：当前字幕检测
    const statusBox = el('div', 'card');
    const sHead = el('div', 'card-head');
    sHead.appendChild(el('h3', null, t('当前字幕状态', '这集有没有字幕')));
    statusBox.appendChild(sHead);
    const sBody = el('div', 'card-body');
    sBody.appendChild(spinner(t('正在检查现有字幕轨…', '正在检查字幕…')));
    statusBox.appendChild(sBody);
    stage.appendChild(statusBox);

    const checkSubs = async () => {
      try {
        const res = await api('/api/tools/nas_check_subtitle', { method: 'POST', body: { args: { path: entry.path } } });
        sBody.textContent = '';
        const subtitle = res.subtitle || {};
        const embedded = Array.isArray(subtitle.embedded) ? subtitle.embedded : [];
        const external = Array.isArray(subtitle.external) ? subtitle.external : [];
        const status = res.language_status
          || (res.has_chinese_subtitle ? 'known_zh' : (res.has_any_subtitle ? 'unknown' : 'none'));
        const unknownLang = t('语言未知', '语言未知');
        let tone = 'warn';
        let statusIcon = 'alert';
        let text;
        if (status === 'known_zh') {
          tone = 'ok';
          statusIcon = 'check';
          text = t('已有中文字幕', '有中文字幕');
        } else if (status === 'none') {
          text = t('缺少字幕', '缺少字幕');
        } else if (res.has_traditional_subtitle) {
          text = t('未检测到简体中文字幕，仅检测到繁体', '未检测到简体中文，仅检测到繁体');
        } else if (status === 'unknown') {
          text = t('检测到外挂字幕，语言未知', '检测到外挂字幕，语言未知');
        } else {
          text = t('检测到字幕，但非简体中文', '检测到字幕，但不是简体中文');
        }
        const badge = el('span', 'badge ' + tone);
        badge.appendChild(icon(statusIcon));
        badge.appendChild(document.createTextNode(text));
        sBody.appendChild(badge);

        if (embedded.length) {
          const embWrap = el('div', 'muted', t('内嵌字幕：', '内嵌字幕：') + embedded.map((s) => `${s.language || unknownLang}(${s.codec || s.format || 'text'})`).join('、'));
          embWrap.style.marginTop = '6px';
          sBody.appendChild(embWrap);
        }
        if (external.length) {
          const extWrap = el('div', 'muted', t('外挂字幕：', '同目录外挂字幕：') + external.map((s) => {
            const langs = Array.isArray(s.languages) && s.languages.length ? s.languages.join('/') : unknownLang;
            return `${s.name || ''}（${langs}）`;
          }).join('、'));
          extWrap.style.marginTop = '4px';
          sBody.appendChild(extWrap);
        }
      } catch (err) {
        sBody.textContent = '';
        sBody.appendChild(errorBox(err));
      }
    };
    checkSubs();

    // 区块 2：搜字幕并一键混流
    const searchCard = el('article', 'card');
    const scHead = el('div', 'card-head');
    scHead.appendChild(el('h3', null, t('搜索 SubHD 字幕并一键混流', '搜索字幕')));
    searchCard.appendChild(scHead);
    const scBody = el('div', 'card-body');

    const searchRow = el('div', 'toolbar');
    const searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.value = cleanMediaTitle(entry.name || entry.path.split('/').pop());
    searchInput.placeholder = t('输入影片/剧集名称关键词…', '输入关键词搜字幕…');
    searchRow.appendChild(searchInput);

    const searchBtn = el('button', 'primary');
    btnContent(searchBtn, 'search', t('搜索字幕', '搜索'));
    searchBtn.type = 'button';
    searchRow.appendChild(searchBtn);

    const autoFillBtn = el('button', 'ghost');
    btnContent(autoFillBtn, 'sparkle', t('自动补全 (media_fill_subtitles)', '自动补字幕'));
    autoFillBtn.type = 'button';
    autoFillBtn.title = t('调用后台多数据源自动寻找字幕', '让系统自动去各站点找字幕');
    searchRow.appendChild(autoFillBtn);
    scBody.appendChild(searchRow);

    const subResultHolder = el('div');
    subResultHolder.style.marginTop = '12px';
    scBody.appendChild(subResultHolder);
    searchCard.appendChild(scBody);
    stage.appendChild(searchCard);

    const startFillJob = async (args, out) => {
      const created = await api('/api/jobs', { method: 'POST', body: { tool: 'media_fill_subtitles', args } });
      pollJobsIndicator();
      return pollJob(created.job.id, out);
    };

    const countPlanned = (res) => {
      for (const k of ['planned', 'to_fill', 'candidates', 'executed', 'filled', 'missing']) {
        if (Array.isArray(res[k])) return res[k].length;
      }
      return 0;
    };

    autoFillBtn.onclick = async () => {
      autoFillBtn.disabled = true;
      subResultHolder.textContent = '';
      subResultHolder.appendChild(spinner(t('正在扫描并生成补字幕计划…', '正在看看哪些能补…')));
      try {
        // 先跑 dry_run 计划，绝不再把「只生成计划」报成「已完成」
        const job = await startFillJob({ path: parentPath(entry.path), dry_run: true }, subResultHolder);
        if (job.status !== 'succeeded') {
          throw new Error((job.error && job.error.message) || t('任务未成功', '任务没跑完'));
        }
        const fillRes = job.result || {};
        const planned = countPlanned(fillRes);
        subResultHolder.textContent = '';
        renderResult(fillRes, subResultHolder, { tool: 'media_fill_subtitles' });
        toast(t('已生成补字幕计划（未改动文件）', '计划出来了，还没动文件'));

        if (!writeGateOff) {
          const runBtn = el('button', 'danger', t('按这个计划真实执行', '就按这个计划补'));
          runBtn.type = 'button';
          runBtn.style.marginTop = '12px';
          runBtn.onclick = async () => {
            const ok = await confirmDialog({
              title: t('确认真实执行补字幕？', '真的开始补字幕？'),
              intro: t('这一步会把字幕写入文件夹。', '这一步会真的把字幕存进文件夹。'),
              items: planned ? [t('预计处理 ' + planned + ' 个文件', '大概会处理 ' + planned + ' 个文件')] : [],
              confirmText: t('开始执行', '开始补'),
              danger: true,
            });
            if (!ok) return;
            runBtn.disabled = true;
            const status = el('div');
            status.style.marginTop = '12px';
            subResultHolder.appendChild(status);
            try {
              const real = await startFillJob({ path: parentPath(entry.path), dry_run: false }, status);
              status.textContent = '';
              if (real.status === 'succeeded' || jobPartial(real)) {
                renderResult(real.result || {}, status, { tool: 'media_fill_subtitles' });
                if (jobPartial(real)) {
                  toast(t('已取消，已完成的部分保留', '取消了，做完的部分都留着'));
                } else {
                  toast(t('补字幕执行完成', '补字幕做完了'));
                  checkSubs();
                }
              } else {
                status.appendChild(errorBox({ code: 'internal_error', message: (real.error && real.error.message) || '任务失败' }));
              }
            } catch (err) {
              status.textContent = '';
              status.appendChild(errorBox(err));
            } finally {
              runBtn.disabled = false;
            }
          };
          subResultHolder.appendChild(runBtn);
        } else {
          subResultHolder.appendChild(el('p', 'muted', t(
            '当前未开启「允许改动文件」，只能预览；到「配置」页开启后即可执行。',
            '现在只能预览，去「配置」页打开「允许改动文件」就能真的执行。',
          )));
        }
        checkSubs();
      } catch (err) {
        subResultHolder.textContent = '';
        subResultHolder.appendChild(errorBox(err));
      } finally {
        autoFillBtn.disabled = false;
      }
    };

    const doSearch = async () => {
      const q = searchInput.value.trim();
      if (!q) { toast(t('请输入搜索关键词', '先写点关键词'), true); return; }
      searchBtn.disabled = true;
      subResultHolder.textContent = '';
      subResultHolder.appendChild(spinner(t('正在搜索 SubHD 字幕…', '正在找字幕…')));
      try {
        const res = await api('/api/tools/subhd_search', { method: 'POST', body: { args: { keyword: q, video_path: entry.path } } });
        subResultHolder.textContent = '';
        if (res && res.ok === false) {
          const msg = (res.error && res.error.message) || t('字幕搜索失败', '搜索失败');
          subResultHolder.appendChild(errorBox(new Error(msg)));
          toast(t('搜索失败：' + msg, '搜索失败：' + msg), true);
          return;
        }
        const list = Array.isArray(res.candidates) ? res.candidates : (Array.isArray(res.results) ? res.results : []);
        if (!list.length) {
          subResultHolder.appendChild(emptyState({
          title: t('未找到匹配字幕。', '没有搜到字幕'),
          hint: t('可以尝试精简搜索词再试一次。', '试着删掉几个字再搜搜看。'),
          action: { label: t('重试', '再搜一次'), onClick: () => doSearch() },
        }));
          return;
        }

        list.slice(0, 15).forEach((item) => {
          const row = el('div', 'media-sub-candidate');
          const rinfo = el('div');
          const title = el('div', 'media-sub-title', item.title || item.name || '字幕候选');
          const meta = el('div', 'media-sub-meta', `${item.languages || item.lang || '中文'} · ${item.format || item.ext || 'srt'} · 评分: ${item.score || '-'}`);
          rinfo.append(title, meta);
          row.appendChild(rinfo);

          const rActions = el('div');
          rActions.style.display = 'flex';
          rActions.style.gap = '6px';

          const muxBtn = el('button', 'primary small');
          btnContent(muxBtn, 'bolt', t('下载并混流进视频', '一键合进视频'));
          muxBtn.type = 'button';
          muxBtn.onclick = async () => {
            if (writeGateOff) {
              toast(t('未开启「允许改动文件」，不能下载或混流', '还没开改动权限，下载和合并都做不了'), true);
              return;
            }
            muxBtn.disabled = true;
            setBtnText(muxBtn, t('正在处理…', '处理中…'));
            try {
              const sid = String(item.sid || item.id || '');
              const dlRes = await api('/api/tools/subhd_download', { method: 'POST', body: { args: { video_path: entry.path, sid } } });
              if (!dlRes || dlRes.ok === false) {
                const msg = (dlRes && dlRes.error && dlRes.error.message) || t('字幕下载失败', '下载失败');
                toast(t('下载失败：' + msg, '下载失败：' + msg), true);
                return;
              }
              const subPath = dlRes.subtitle_path || firstPath(dlRes);
              if (!subPath) {
                toast(t('字幕已下载，但未获取到文件路径', '字幕已下载，但没拿到文件路径'), true);
                return;
              }
              toast(t('字幕已下载，正在混流进视频…', '下载完成，正在合并…'));
              // 混流是长任务：走后端作业，弹窗里显示真实进度（已等待多少秒）
              const status = el('div');
              status.style.marginTop = '10px';
              subResultHolder.appendChild(status);
              const created = await api('/api/jobs', { method: 'POST', body: { tool: 'media_mux_subtitles', args: { video_path: entry.path, dry_run: false } } });
              pollJobsIndicator();
              const job = await pollJob(created.job.id, status);
              status.textContent = '';
              const muxRes = job.status === 'succeeded' ? (job.result || {}) : null;
              if (!muxRes) {
                throw new Error((job.error && job.error.message) || t('混流任务失败', '合并任务失败了'));
              }
              if (!muxRes || muxRes.ok === false) {
                const msg = (muxRes && muxRes.error && muxRes.error.message) || t('混流失败', '合并失败');
                throw new Error(msg);
              }
              const executed = Array.isArray(muxRes.executed) ? muxRes.executed : [];
              if (!executed.length) {
                const errs = Array.isArray(muxRes.errors) ? muxRes.errors : [];
                const reason = errs.length
                  ? String(errs[0].message || errs[0].error || errs[0])
                  : t('未找到可混流的外挂字幕（可能已存在内嵌中文字幕）', '没有可合并的外挂字幕');
                toast(t('混流未执行：' + reason, '没有合并：' + reason), true);
                return;
              }
              toast(t('字幕已成功混流进原视频！', '字幕已合并进视频！'));
              checkSubs();
            } catch (err) {
              toast(t('混流失败：' + (err.message || '未知错误'), '合并失败'), true);
            } finally {
              muxBtn.disabled = false;
              setBtnText(muxBtn, t('下载并混流进视频', '一键合进视频'));
            }
          };

          const dlBtn = el('button', 'ghost small', t('仅下载外挂', '仅下载外挂'));
          dlBtn.type = 'button';
          dlBtn.onclick = async () => {
            dlBtn.disabled = true;
            try {
              const sid = String(item.sid || item.id || '');
              const dlRes = await api('/api/tools/subhd_download', { method: 'POST', body: { args: { video_path: entry.path, sid } } });
              if (!dlRes || dlRes.ok === false) {
                const msg = (dlRes && dlRes.error && dlRes.error.message) || t('字幕下载失败', '下载失败');
                toast(t('下载失败：' + msg, '下载失败：' + msg), true);
                return;
              }
              toast(t('字幕下载成功！', '字幕已下载'));
              checkSubs();
            } catch (err) {
              toast(t('下载失败：' + (err.message || '未知错误'), '下载失败'), true);
            } finally {
              dlBtn.disabled = false;
            }
          };

          if (writeGateOff) {
            muxBtn.disabled = true;
            dlBtn.disabled = true;
            muxBtn.title = dlBtn.title = t('需要先在「配置」页开启「允许改动文件」', '先去「配置」页打开「允许改动文件」');
          }
          rActions.append(muxBtn, dlBtn);
          row.appendChild(rActions);
          subResultHolder.appendChild(row);
        });
      } catch (err) {
        subResultHolder.textContent = '';
        subResultHolder.appendChild(errorBox(err));
      } finally {
        searchBtn.disabled = false;
      }
    };

    searchBtn.onclick = doSearch;
    searchInput.onkeydown = (e) => { if (e.key === 'Enter') doSearch(); };
  };

  // 渲染【规范命名与整理】
  const renderOrganizeTab = () => {
    stage.textContent = '';
    const card = el('article', 'card');
    const ch = el('div', 'card-head');
    ch.appendChild(el('h3', null, t('整理与重命名预览 (Dry-run)', '规范整理预览')));
    card.appendChild(ch);
    const cb = el('div', 'card-body');
    cb.appendChild(el('p', 'muted', t('将对该媒体所在文件夹进行规范化命名检测，默认只预览计划，不修改任何文件。', '检查这个视频所在文件夹能否规范命名，默认只预览，不动文件。')));

    const actRow = el('div', 'form-actions');
    actRow.style.marginTop = '12px';
    const previewBtn = el('button', 'primary', t('👀 预览整理计划 (安全 Dry-run)', '预览整理'));
    previewBtn.type = 'button';
    actRow.appendChild(previewBtn);
    cb.appendChild(actRow);

    const outHolder = el('div');
    outHolder.style.marginTop = '14px';
    cb.appendChild(outHolder);
    card.appendChild(cb);
    stage.appendChild(card);

    previewBtn.onclick = async () => {
      previewBtn.disabled = true;
      outHolder.textContent = '';
      outHolder.appendChild(spinner(t('正在生成规范整理计划…', '正在分析…')));
      try {
        const res = await api('/api/tools/organize_media', {
          method: 'POST',
          body: { args: { path: parentPath(entry.path), dry_run: true } },
        });
        outHolder.textContent = '';
        renderResult(res, outHolder, { tool: 'organize_media' });

        const confirmRow = el('div', 'form-actions');
        confirmRow.style.marginTop = '14px';
        const applyBtn = el('button', 'danger');
        btnContent(applyBtn, 'alert', t('确认无误，执行真实整理', '确认整理并改名'));
        applyBtn.type = 'button';
        applyBtn.onclick = async () => {
          if (!confirm(t('即将对磁盘上的文件执行重命名与整理，确认执行？', '确定要真正改动文件吗？'))) return;
          applyBtn.disabled = true;
          try {
            const realRes = await api('/api/tools/organize_media', {
              method: 'POST',
              body: { args: { path: parentPath(entry.path), dry_run: false } },
            });
            toast(t('整理执行完成！', '整理完成！'));
            outHolder.textContent = '';
            renderResult(realRes, outHolder, { tool: 'organize_media' });
          } catch (rErr) {
            toast(t('执行失败：' + rErr.message, '整理失败'), true);
          } finally {
            applyBtn.disabled = false;
          }
        };
        confirmRow.appendChild(applyBtn);
        outHolder.appendChild(confirmRow);
      } catch (err) {
        outHolder.textContent = '';
        outHolder.appendChild(errorBox(err));
      } finally {
        previewBtn.disabled = false;
      }
    };
  };

  // 渲染【画质与体检】
  const renderQualityTab = () => {
    stage.textContent = '';
    const card = el('article', 'card');
    const ch = el('div', 'card-head');
    ch.appendChild(el('h3', null, t('音视频画质与完整性检测', '体检')));
    card.appendChild(ch);
    const cb = el('div', 'card-body');

    const actRow = el('div', 'form-actions');
    const qBtn = el('button', 'primary', t('📊 分析画质与音轨 (media_quality_analyze)', '查看画质音轨'));
    qBtn.type = 'button';
    const iBtn = el('button', 'ghost', t('🛡️ 完整性检查 (media_integrity_check)', '检查文件是否损坏'));
    iBtn.type = 'button';
    actRow.append(qBtn, iBtn);
    cb.appendChild(actRow);

    const outHolder = el('div');
    outHolder.style.marginTop = '14px';
    cb.appendChild(outHolder);
    card.appendChild(cb);
    stage.appendChild(card);

    qBtn.onclick = async () => {
      qBtn.disabled = true;
      outHolder.textContent = '';
      outHolder.appendChild(spinner(t('正在探测视频编码与音画质…', '正在分析…')));
      try {
        const res = await api('/api/tools/media_quality_analyze', { method: 'POST', body: { args: { path: entry.path } } });
        outHolder.textContent = '';
        renderResult(res, outHolder, { tool: 'media_quality_analyze' });
      } catch (err) {
        outHolder.textContent = '';
        outHolder.appendChild(errorBox(err));
      } finally {
        qBtn.disabled = false;
      }
    };

    iBtn.onclick = async () => {
      iBtn.disabled = true;
      outHolder.textContent = '';
      outHolder.appendChild(spinner(t('正在检查视频文件完整性…', '正在检查…')));
      try {
        const res = await api('/api/tools/media_integrity_check', { method: 'POST', body: { args: { path: entry.path } } });
        outHolder.textContent = '';
        renderResult(res, outHolder, { tool: 'media_integrity_check' });
      } catch (err) {
        outHolder.textContent = '';
        outHolder.appendChild(errorBox(err));
      } finally {
        iBtn.disabled = false;
      }
    };
  };

  switchTab('sub');

  // 底部动作
  const foot = el('div', 'modal-foot');
  const sendBtn = el('button', 'ghost', t('带着路径去工具台 →', '去工具台'));
  sendBtn.type = 'button';
  sendBtn.onclick = () => {
    close();
    sendToTool('nas_check_subtitle', { path: entry.path });
  };
  foot.appendChild(sendBtn);

  const doneBtn = el('button', 'primary', t('关闭', '关闭'));
  doneBtn.type = 'button';
  doneBtn.onclick = () => close();
  foot.appendChild(doneBtn);
  box.appendChild(foot);

  overlay.appendChild(box);
  document.body.appendChild(overlay);

  const releaseTrap = modalA11y(overlay, box, t('单集快捷操作', '这集的快捷操作') + ' ' + (entry.name || ''));
  function close() {
    window.removeEventListener('keydown', handleKey);
    releaseTrap();
    overlay.remove();
  }

  const handleKey = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  };
  window.addEventListener('keydown', handleKey);
  overlay.onclick = (e) => {
    if (e.target === overlay) close();
  };
}


// ---------------------------------------------------------------- 结果接力
// 上一步结果一键带入下一步工具，消除“复制路径→切页→粘贴”的手工衔接。
function parentPath(p) {
  const text = String(p || '');
  const idx = text.lastIndexOf('/');
  return idx > 0 ? text.slice(0, idx) : text;
}

// 从工具结果里找一个可用的路径（标量优先，其次常见数组的首行）
function firstPath(result) {
  if (!result || typeof result !== 'object') return '';
  for (const key of ['path', 'video_path', 'subtitle_path', 'best_path']) {
    if (typeof result[key] === 'string' && result[key]) return result[key];
  }
  for (const key of ['videos', 'files', 'matches', 'entries', 'candidates', 'seasons']) {
    const arr = result[key];
    if (Array.isArray(arr) && arr.length && arr[0] && typeof arr[0].path === 'string') return arr[0].path;
  }
  return '';
}

// 行级接力：HANDOFFS_ROWS[上游工具][结果数组key](src, row, ctx) => [{label, tool, args}]
const HANDOFFS_ROWS = {
  nas_find_files: {
    matches: (src, row, ctx) => {
      const p = (src && src.path) || row.path || '';
      if (!p) return [];
      const dir = parentPath(p);
      return [
        { label: t('浏览所在目录', '打开文件夹'), tool: 'nas_list_directory', args: { path: dir } },
        { label: t('批量补字幕', '给它补字幕'), tool: 'media_fill_subtitles', args: { path: dir } },
      ];
    },
  },
  subhd_search: {
    candidates: (src, row, ctx) => {
      const vp = ctx && ctx.args && ctx.args.video_path;
      const sid = (src && (src.sid || src.id)) || row.sid;
      if (!vp || !sid) return [];
      return [
        { label: t('下载这条字幕', '下载这条'), tool: 'subhd_download', args: { video_path: vp, sid: String(sid) } },
      ];
    },
  },
};

// 结果头接力：HANDOFFS_RESULT[上游工具](result, ctx.args) => [{label, tool, args}]
const HANDOFFS_RESULT = {
  nas_scan_media: (result) => {
    const p = firstPath(result);
    if (!p) return [];
    return [
      { label: t('按这里查字幕', '查字幕'), tool: 'nas_check_subtitle', args: { path: p } },
      { label: t('按这里补字幕', '补字幕'), tool: 'media_fill_subtitles', args: { path: parentPath(p) } },
    ];
  },
  media_quality_analyze: (result) => {
    const p = firstPath(result);
    return p ? [{ label: t('按这里查字幕', '查字幕'), tool: 'nas_check_subtitle', args: { path: p } }] : [];
  },
  subhd_download: (result) => {
    const p = result.video_path || firstPath(result);
    return p ? [{ label: t('检查该视频字幕', '查字幕'), tool: 'nas_check_subtitle', args: { path: p } }] : [];
  },
  media_fill_subtitles: (result) => {
    const p = firstPath(result);
    return p ? [{ label: t('检查补全结果', '查字幕'), tool: 'nas_check_subtitle', args: { path: p } }] : [];
  },
};

// ---------------------------------------------------------------- 预设流水线
// 纯前端串行：上一步结果注入下一步；不自动执行非 dry_run 的写操作。
// steps: { tool, argsFrom(prevResult, base) => args | null(null=缺参数，停下交给用户) }
const FLOWS = [
  {
    id: 'checkup',
    title: '体检流水线',
    titleFriendly: '给媒体库做一次全面体检',
    input: 'path',
    steps: [
      { tool: 'nas_disk_usage', argsFrom: (prev, base) => ({ path: base.path || '' }) },
      { tool: 'nas_scan_media', argsFrom: (prev, base) => ({ path: base.path || '' }) },
      { tool: 'nas_check_subtitle', argsFrom: (prev, base) => ({ path: base.path || '' }) },
    ],
  },
  {
    id: 'fill-sub',
    title: '补字幕向导',
    titleFriendly: '查缺哪些字幕 → 生成补全计划（不自动动手）',
    input: 'path',
    steps: [
      { tool: 'nas_check_subtitle', argsFrom: (prev, base) => ({ path: base.path || '' }) },
      { tool: 'media_fill_subtitles', argsFrom: (prev, base) => ({ path: base.path || '', dry_run: true }) },
    ],
  },
  {
    id: 'hunt',
    title: '资源横搜',
    titleFriendly: '一个关键词，同时搜聚合站和 Jackett',
    input: 'query',
    steps: [
      { tool: 'media_search', argsFrom: (prev, base) => (base.query ? { query: base.query } : null) },
      { tool: 'jackett_search', argsFrom: (prev, base) => (base.query ? { query: base.query } : null) },
    ],
  },
];

function pick(map, key, fallback) {
  return IS_PRO() || !map[key] ? fallback : map[key];
}

const PATH_FIELDS = /(^|_)(path|paths|root|roots|dir|directory|target|source|dest|destination)$/;
// 目录型参数（path / root）在界面上做成多选勾选条；video_path 这类单文件参数保持单选
const MULTI_PATH_NAMES = new Set(['path', 'root']);
// 例外：这几个工具的 path 指向单个视频文件或用于浏览，批量没有意义
const MULTI_PATH_SKIP = new Set(['media_quality_analyze', 'nas_check_subtitle', 'nas_list_directory']);
const LIST_FIELDS = /(files|queries|keywords|names|titles|paths|extensions|tracks|urls|episodes|words)/;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

// ---------------------------------------------------------------- 图标系统
// 统一内联单线图标：24 网格、18×18 视觉尺寸、1.5 描边，颜色跟随 currentColor。
// 不引图标字体/外部资源，暗色主题自动成立。
const ICON_PATHS = {
  overview: 'M4 11l8-7 8 7 M6 10v10h12V10',
  browse: 'M3.5 6.5a1.5 1.5 0 0 1 1.5-1.5h4l2 2h8A1.5 1.5 0 0 1 20.5 8.5v9A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z',
  scan: 'M3 12h4l2.5-6 4.5 12 2.5-6H21',
  subtitle: 'M6 3.5h8l4 4v13H6z M14 3.5v4h4 M9 12h6 M9 15.5h4',
  resource: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M15.5 8.5l-2 5-5 2 2-5z',
  organize: 'M4 8.5h13 M13.5 5l3.5 3.5-3.5 3.5 M20 15.5H7 M10.5 12L7 15.5l3.5 3.5',
  config: 'M4 7h7 M15 7h5 M4 17h5 M13 17h7 M13 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4z M9 15a2 2 0 1 0 0 4 2 2 0 0 0 0-4z',
  workbench: 'M3.5 5.5h17v13h-17z M7.5 9.5l2.5 2.5-2.5 2.5 M12.5 14.5h4',
  search: 'M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13z M15.5 15.5L20.5 20.5',
  theme: 'M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z',
  refresh: 'M20 12a8 8 0 1 1-2.4-5.7 M20 3.5v4h-4',
  health: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M8 12.2l2.7 2.7L16.5 9',
  jobs: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7.5V12l3 2',
  menu: 'M4 7h16 M4 12h16 M4 17h16',
  close: 'M6.5 6.5l11 11 M17.5 6.5l-11 11',
  chevron: 'M9.5 7l5 5-5 5',
  folder: 'M3.5 6.5a1.5 1.5 0 0 1 1.5-1.5h4l2 2h8A1.5 1.5 0 0 1 20.5 8.5v9A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z',
  parent: 'M9 14l-5-5 5-5 M4 9h7.5a5 5 0 0 1 5 5v6',
  film: 'M3.5 5.5h17v13h-17z M7.5 5.5v13 M16.5 5.5v13',
  file: 'M6 3.5h8l4 4v13H6z M14 3.5v4h4',
  bolt: 'M13 3L5.5 13.5H11l-1 7.5 8-11h-5.5z',
  rollback: 'M4 12a8 8 0 1 0 2.4-5.7 M4 3.5v4h4 M12 8v4.5l3 1.5',
  alert: 'M12 4.5l8 14H4z M12 10v4 M12 16.8v.01',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  archive: 'M3.5 5.5h17v4h-17z M5 9.5h14v9H5z M9.5 12.5h5',
  sparkle: 'M12 4l2 6 6 2-6 2-2 6-2-6-6-2 6-2z',
  dot: 'M12 12h.01',
};

const SVG_NS = 'http://www.w3.org/2000/svg';

function icon(name, cls) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '18');
  svg.setAttribute('height', '18');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.5');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICON_PATHS[name] || ICON_PATHS.dot);
  svg.appendChild(path);
  return svg;
}

// 图标 + 文案：统一间距，避免每处手写空格
function iconLabel(name, text, cls) {
  const node = el('span', 'icon-label' + (cls ? ' ' + cls : ''));
  node.appendChild(icon(name));
  if (text !== undefined && text !== null) node.appendChild(document.createTextNode(String(text)));
  return node;
}

// 按钮「图标 + 可更新文案」：只改 .btn-label，图标不会被 textContent 抹掉
function btnContent(btn, name, text) {
  const wrap = el('span', 'icon-label');
  wrap.appendChild(icon(name));
  const lab = el('span', 'btn-label', text);
  wrap.appendChild(lab);
  btn.appendChild(wrap);
  return lab;
}

function setBtnText(btn, text) {
  const lab = btn && btn.querySelector('.btn-label');
  if (lab) lab.textContent = String(text);
  else if (btn) btn.textContent = String(text);
}

// 把 index.html 里的 [data-icon] 占位换成 SVG
function mountIcons(root) {
  (root || document).querySelectorAll('[data-icon]').forEach((slot) => {
    if (slot.querySelector('.icon')) return;
    slot.appendChild(icon(slot.dataset.icon));
  });
}

let __uid = 0;
function nextId(prefix) {
  __uid += 1;
  return (prefix || 'ui') + '-' + __uid;
}

// ---------------------------------------------------------------- 模态无障碍
// 弹窗统一：role=dialog + aria-modal，Tab 在弹窗内循环，关闭后焦点回到打开前的元素。
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
function trapFocus(overlay) {
  const prev = document.activeElement;
  const onKey = (ev) => {
    if (ev.key !== 'Tab') return;
    const nodes = [...overlay.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (!overlay.contains(active)) {
      ev.preventDefault();
      (ev.shiftKey ? last : first).focus();
    } else if (ev.shiftKey && active === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && active === last) {
      ev.preventDefault();
      first.focus();
    }
  };
  document.addEventListener('keydown', onKey, true);
  return () => {
    document.removeEventListener('keydown', onKey, true);
    if (prev && prev.focus) {
      try { prev.focus(); } catch (_) { /* 原元素已移除时忽略 */ }
    }
  };
}
let openModalCount = 0;
function modalA11y(overlay, box, label) {
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  if (label) box.setAttribute('aria-label', label);
  openModalCount += 1;
  document.body.classList.add('modal-open');
  const release = trapFocus(overlay);
  return () => {
    release();
    openModalCount = Math.max(0, openModalCount - 1);
    if (!openModalCount) document.body.classList.remove('modal-open');
  };
}

function lines(value) {
  return String(value || '').split(/[\n,]/).map((v) => v.trim()).filter(Boolean);
}

function firstAllowedPath() {
  const cfg = S.config || {};
  const list = cfg.allowed_paths_expanded || cfg.allowed_paths || [];
  return list[0] || '/media';
}

// 目录条目判断：API 里 type 可能是 dir 或 directory，个别场景用 is_dir 布尔
function isDirEntry(entry) {
  return !!entry && (entry.type === 'dir' || entry.type === 'directory' || entry.is_dir === true);
}

function fmtSize(bytes) {
  const n = Number(bytes);
  if (!isFinite(n) || n < 0) return String(bytes);
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return (v >= 100 ? v.toFixed(0) : v.toFixed(v >= 10 ? 1 : 2)) + ' ' + units[i];
}

function fmtDuration(sec) {
  const n = Number(sec);
  if (!isFinite(n) || n <= 0) return String(sec);
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = Math.round(n % 60);
  return h ? `${h}h${String(m).padStart(2, '0')}m` : `${m}m${String(s).padStart(2, '0')}s`;
}

function fmtTime(value) {
  const text = String(value);
  const d = new Date(text);
  if (isNaN(d.getTime())) return text;
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtCell(value, key) {
  if (value === true) return '是';
  if (value === false) return '否';
  if (value === null || value === undefined) return '';
  const k = String(key || '');
  if (k === 'size_bytes' || k === 'size' || k === '大小') return fmtSize(value);
  if (k === 'mtime' || k.endsWith('_at') || k.endsWith('时间')) return fmtTime(value);
  if (k === 'duration_seconds' || k === 'duration_ms' || k === '时长') return k === 'duration_ms' ? (Number(value) / 1000).toFixed(1) + 's' : fmtDuration(value);
  if (k === '耗时') return (Number(value) / 1000).toFixed(1) + 's';
  if (k === 'percent_used') return Number(value).toFixed(1) + '%';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2);
  const text = String(value);
  return text.length > 160 ? text.slice(0, 160) + '…' : text;
}

// 数值列：数字右对齐，单位单独降一档展示（只做展示分层，不改原值）
const NUMERIC_COLS = new Set([
  '大小', '总量', '可用', '剩余', '使用率', '耗时', '时长', '码率', '帧率', '评分',
  '做种', '置信度', '数量', '文件数', '集数', '季数', '结果数', '最高分', '返回',
  'size', 'size_bytes', 'total_bytes', 'used_bytes', 'free_bytes', 'percent_used',
  'duration_ms', 'duration_seconds', 'bit_rate', 'frame_rate', 'score', 'seeders',
  'file_count', 'episode_count', 'season_count', 'max_list_entries', 'max_search_results',
  'max_scan_files', 'max_list_depth', 'max_errors_reported',
]);
// 只拆常见单位，日期、编号等含空格/冒号的文本不动
const CELL_UNIT = /^([\d.,]+)\s*(B|KB|MB|GB|TB|PB|%|ms|s)$/;

function cellContent(text, numeric) {
  const cell = el('span', 'cell');
  const raw = String(text === null || text === undefined ? '' : text);
  if (numeric) {
    const m = raw.match(CELL_UNIT);
    if (m) {
      cell.appendChild(el('span', 'num-val', m[1]));
      cell.appendChild(el('span', 'num-unit', m[2]));
      return cell;
    }
  }
  cell.textContent = raw;
  return cell;
}

function isNumericCol(col, value) {
  return typeof value === 'number' || NUMERIC_COLS.has(col)
    || /_(bytes|human)$/.test(col);
}

function label(key) {
  const leaf = String(key).split('.').pop();
  if (!IS_PRO() && FRIENDLY.field[leaf]) return FRIENDLY.field[leaf];
  return LABELS[leaf] || LABELS[key] || leaf;
}

async function api(path, options = {}) {
  const opts = { headers: {}, credentials: 'same-origin', ...options };
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
    opts.headers['Content-Type'] = 'application/json';
  }
  if (opts.method && opts.method !== 'GET') opts.headers['X-Nas-App'] = '1';
  const res = await fetch(path, opts);
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = { ok: false, error: { code: 'bad_response', message: text.slice(0, 300) } }; }
  }
  if (res.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.hash);
    throw new Error('登录已失效');
  }
  if (!res.ok) {
    const err = (data && data.error) || { code: 'http_' + res.status, message: '请求失败 HTTP ' + res.status };
    const e = new Error(err.message || '请求失败');
    e.code = err.code;
    e.payload = data;
    throw e;
  }
  return data;
}

function toast(message, isError) {
  const node = document.getElementById('toast');
  if (!node) return;
  node.textContent = '';
  node.appendChild(el('span', 'toast-msg', message));
  const close = el('button', 'toast-close');
  close.type = 'button';
  close.setAttribute('aria-label', t('关闭提示', '关闭提示'));
  close.appendChild(icon('close'));
  close.onclick = () => { clearTimeout(toast._timer); node.hidden = true; };
  node.appendChild(close);
  node.className = 'toast' + (isError ? ' err' : '');
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, isError ? 6000 : 3200);
}

// ---------------------------------------------------------------- 界面风格与配色

const THEME_KEY = 'nas-color-theme';
const THEME_LABELS = { auto: '自动', light: '浅色', dark: '深色' };
const themeMq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

function themePref() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' ? v : 'auto';
  } catch (_) { return 'auto'; }
}

function resolveTheme(pref) {
  return pref === 'auto' ? (themeMq && themeMq.matches ? 'dark' : 'light') : pref;
}

function applyTheme() {
  const pref = themePref();
  document.documentElement.dataset.theme = resolveTheme(pref);
  const themeLabel = document.getElementById('theme-label');
  if (themeLabel) themeLabel.textContent = t('配色 · ' + THEME_LABELS[pref], '配色 · ' + THEME_LABELS[pref]);
  const meta = document.getElementById('theme-color');
  if (meta) {
    const dark = resolveTheme(pref) === 'dark';
    const light = '#f4f1ea';
    meta.setAttribute('content', dark ? '#0e1219' : light);
  }
}

function cycleTheme() {
  const order = ['auto', 'light', 'dark'];
  const next = order[(order.indexOf(themePref()) + 1) % order.length];
  try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* 忽略 */ }
  applyTheme();
  toast(t(`配色已切换为「${THEME_LABELS[next]}」`, `配色：${THEME_LABELS[next]}`));
}

function applyShellCopy() {
  if (!S.meta) return;
  const badge = document.getElementById('auth-badge');
  badge.textContent = S.meta.auth_required
    ? t('令牌保护已开启', '已设置访问密码')
    : t('未设置令牌', '未设置访问密码');
  badge.className = 'badge ' + (S.meta.auth_required ? 'ok' : 'warn');
}

// 错误码 → 主行动按钮：能补配置就去配置，能重试就重试，避免只会说「失败了」
function errorAction(code, retry) {
  if (/^(access_denied|permission_denied|not_configured|site_disabled|auth_failed|config_error|write_disabled)$/.test(code)) {
    const target = code === 'access_denied' || code === 'permission_denied' ? '去配置打开权限' : '去配置检查设置';
    return { label: t(target, target), onClick: () => { location.hash = '#/config'; } };
  }
  if (code === 'unauthorized' || code === 'csrf') {
    return { label: t('重新登录', '重新登录'), onClick: () => { location.href = '/login'; } };
  }
  if (typeof retry === 'function') return { label: t('重试', '再试一次'), onClick: retry };
  return { label: t('重试', '刷新再试'), onClick: () => route() };
}

// 统一失败态：人话主文案 + 一个主行动按钮，技术错误码/原始信息折叠到「详情」
function errorBox(error, opts = {}) {
  if (typeof error === 'string') error = { code: error, message: error };
  const code = (error && error.code) || 'error';
  const message = (error && error.message) || '未知错误';
  const extras = Object.fromEntries(Object.entries(error || {}).filter(([k, v]) => !['code', 'message'].includes(k) && v != null));
  const friendly = pick(FRIENDLY.err, code, null);
  // 有些后端把「站点没开启」报成 invalid_arguments，靠消息里的开关/密钥线索改判为未配置
  const configIssue = /(enabled=false|未启用|未配置|not configured|api_key|请配置)/i.test(message || '');
  let title = configIssue ? t('这个功能还没配置好', '这个功能还没配置好') : (friendly || t('这次没有完成', '这次没有成功'));
  let hint = '';
  if (configIssue) {
    hint = t('到「配置」页把对应的开关或密钥填上，再回来试。', '到「配置」页把对应的开关或密钥填上，再回来试。');
  } else if (friendly) {
    if (message && message !== friendly && message !== title) hint = message;
  } else {
    hint = t('可以直接重试；如果反复出现，展开「详情」查看错误信息。', '可以先再试一次；如果一直不行，展开「详情」看看原因。');
  }
  const action = configIssue
    ? { label: t('去配置检查设置', '去配置检查设置'), onClick: () => { location.hash = '#/config'; } }
    : errorAction(code, opts.retry);
  return emptyState({
    title: title,
    hint: hint,
    icon: 'alert',
    tone: 'err',
    action: action,
    details: { code: code, message: message, ...extras },
    detailsLabel: t('详情', '详情'),
  });
}

function rawDetails(data) {
  const details = el('details', 'raw');
  details.appendChild(el('summary', null, t('查看原始数据', '查看原始数据')));
  const pre = el('pre', 'json');
  pre.textContent = JSON.stringify(data, null, 2);
  details.appendChild(pre);
  return details;
}

function spinner(text) {
  const wrap = el('div', 'inline-spin');
  wrap.appendChild(el('span', 'spinner'));
  wrap.appendChild(el('span', null, text || t('执行中…', '正在处理…')));
  return wrap;
}

// ---------------------------------------------------------------- 路由与外壳
function buildNav() {
  const nav = document.getElementById('nav');
  nav.textContent = '';
  for (const item of NAV) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.page = item.id;
    btn.appendChild(el('span', 'ico')).appendChild(icon(item.icon));
    btn.appendChild(el('span', null, item.label));
    btn.addEventListener('click', () => { location.hash = '#/' + item.id; });
    nav.appendChild(btn);
  }
}

function setActiveNav(pageId) {
  document.querySelectorAll('#nav button').forEach((btn) => {
    const on = btn.dataset.page === pageId;
    btn.classList.toggle('active', on);
    if (on) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  });
}

// 窄屏导航抽屉：≤920px 时侧栏收起为抽屉，汉堡按钮打开，遮罩/Esc/选中导航关闭
function setupNavDrawer() {
  const toggle = document.getElementById('nav-toggle');
  const mask = document.getElementById('nav-mask');
  const sidebar = document.querySelector('aside.sidebar');
  if (!toggle || !mask || !sidebar) return;
  const narrow = () => window.innerWidth <= 920;
  const setOpen = (open) => {
    document.body.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', open ? '关闭导航' : '打开导航');
    mask.hidden = !open;
    if (narrow()) sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (open) {
      const first = sidebar.querySelector('nav button');
      if (first) first.focus();
    } else if (narrow()) {
      toggle.focus();
    }
  };
  if (narrow()) sidebar.setAttribute('aria-hidden', 'true');
  toggle.addEventListener('click', () => setOpen(!document.body.classList.contains('nav-open')));
  mask.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && document.body.classList.contains('nav-open')) setOpen(false);
  });
  document.getElementById('nav').addEventListener('click', (ev) => {
    if (ev.target.closest('button') && document.body.classList.contains('nav-open')) setOpen(false);
  });
  window.addEventListener('resize', () => {
    if (narrow() || !document.body.classList.contains('nav-open')) return;
    document.body.classList.remove('nav-open');
    mask.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    sidebar.removeAttribute('aria-hidden');
  });
}

// 统一的空状态/失败态：说明现在是什么情况 + 一个可点的主行动按钮 + 可折叠的技术详情
function emptyState(title, hint, opts = {}) {
  if (title && typeof title === 'object') { opts = title; title = opts.title; hint = opts.hint; }
  const box = el('div', 'empty' + (opts.tone ? ' empty-' + opts.tone : ''));
  const ico = el('div', 'empty-ico');
  ico.appendChild(icon(opts.icon || 'archive'));
  box.appendChild(ico);
  box.appendChild(el('strong', null, title || ''));
  if (hint) box.appendChild(el('p', 'muted', hint));
  const action = opts.action;
  if (action && action.label) {
    const btn = el('button', 'primary small empty-action', action.label);
    btn.type = 'button';
    btn.addEventListener('click', () => {
      if (typeof action.onClick === 'function') action.onClick();
      else if (action.page) location.hash = '#/' + action.page;
    });
    box.appendChild(btn);
  }
  if (opts.details) {
    const det = el('details', 'tech-detail');
    det.appendChild(el('summary', null, opts.detailsLabel || t('详情', '详情')));
    const pre = el('pre', 'json');
    pre.textContent = typeof opts.details === 'string' ? opts.details : JSON.stringify(opts.details, null, 2);
    det.appendChild(pre);
    box.appendChild(det);
  }
  return box;
}

function setHeader(title, subtitle) {
  document.getElementById('page-title').textContent = title;
  document.getElementById('page-subtitle').textContent = subtitle || '';
}

async function route() {
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
  const hash = location.hash.replace(/^#\/?/, '');
  const [pageId, arg] = hash.split('/');
  // 深链直达工具：#/tool/{工具名} → 落到所属分类页并激活该工具
  if (pageId === 'tool' && arg) { sendToTool(decodeURIComponent(arg)); return; }
  const item = NAV.find((n) => n.id === pageId) || NAV[0];
  S.page = item.id;
  setActiveNav(item.id);
  const view = document.getElementById('view');
  view.textContent = '';
  const loading = spinner(t('正在加载…', '正在打开…'));
  view.appendChild(loading);
  try {
    if (item.id === 'overview') await pageOverview(view);
    else if (item.id === 'config') await pageConfig(view);
    else if (item.id === 'workbench') await pageWorkbench(view);
    else if (item.id === 'browse') await pageBrowse(view, arg ? decodeURIComponent(arg) : null);
    else {
      setHeader(item.label, t('每个工具一个表单；长任务自动转后台队列并轮询结果。', '每个功能一张表单；慢的会自动放到后台处理。'));
      view.appendChild(toolTabs(item));
    }
  } catch (err) {
    view.appendChild(errorBox({ code: err.code || 'load_failed', message: err.message }));
  } finally {
    loading.remove();
    view.classList.remove('in');
    void view.offsetWidth;
    view.classList.add('in');
    doScrollTo();
  }
}

// ---------------------------------------------------------------- 分类页：工具标签切换
// 一次只展示一个卡片，切换时保留各卡片已填内容与结果，避免整页超长滚动。
// tabs: [{ key, title, risk?, note?, build() }]
function tabbedView(id, tabs, activeKey) {
  if (tabs.length <= 1) return tabs.length ? tabs[0].build() : el('div');
  const bar = el('div', 'tool-tabs');
  bar.setAttribute('role', 'group');
  bar.setAttribute('aria-label', '功能切换');
  const stage = el('div', 'tool-stage');
  const cache = new Map();
  const get = (tab) => {
    if (!cache.has(tab.key)) cache.set(tab.key, tab.build());
    return cache.get(tab.key);
  };
  let active = tabs.some((tb) => tb.key === activeKey) ? activeKey : tabs[0].key;
  const show = (key) => {
    active = key;
    S.activeTab[id] = key;
    const tab = tabs.find((tb) => tb.key === key);
    stage.textContent = '';
    stage.appendChild(get(tab));
    bar.querySelectorAll('button').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.key === key));
    });
  };
  for (const tab of tabs) {
    const btn = el('button', 'tool-tab');
    btn.type = 'button';
    btn.dataset.key = tab.key;
    btn.setAttribute('aria-pressed', 'false');
    if (tab.risk) btn.appendChild(el('i', 'rdot ' + tab.risk));
    btn.appendChild(el('span', null, tab.title));
    if (tab.note) btn.appendChild(el('em', null, tab.note));
    if (tab.hint) btn.title = tab.hint;
    btn.addEventListener('click', () => show(tab.key));
    bar.appendChild(btn);
  }
  const wrap = el('div');
  wrap.append(bar, stage);
  show(active);
  return wrap;
}

function toolTabs(item) {
  const tabs = (item.tools || []).filter((n) => S.tools[n]).map((name) => {
    const spec = S.tools[name];
    return {
      key: name,
      title: spec.title,
      risk: spec.risk,
      note: spec.long_running ? t('后台', '慢') : '',
      hint: `${spec.title} · ${spec.risk_label}${spec.long_running ? ' · ' + t('后台任务', '需要多等一会儿') : ''}`,
      build: () => toolCard(name),
    };
  });
  const view = tabbedView(item.id, tabs, S.activeTab[item.id]);
  if (item.id === 'organize') {
    const tip = el('div', 'notice');
    tip.style.marginBottom = '12px';
    tip.style.display = 'flex';
    tip.style.alignItems = 'center';
    tip.style.justifyContent = 'space-between';
    tip.style.gap = '12px';
    const msg = el('div');
    msg.appendChild(el('strong')).appendChild(iconLabel('rollback', t('安全冗余保障：支持随时回滚', '支持随时一键回滚整理')));
    msg.appendChild(el('p', 'muted', t('如果自动化整理或重命名的结果不符合预期，可使用回滚工具将文件恢复原状。', '要是整理改名错了，点右边按钮可以一键恢复成原本的样子。')));
    tip.appendChild(msg);
    const rbBtn = el('button', 'ghost small');
    btnContent(rbBtn, 'rollback', t('回滚上次整理 (media_rollback)', '一键恢复'));
    rbBtn.type = 'button';
    rbBtn.onclick = () => sendToTool('media_rollback', {});
    tip.appendChild(rbBtn);
    view.prepend(tip);
  }
  return view;
}

// ---------------------------------------------------------------- 跨工具衔接
// 带着参数跳到目标工具：写 S.pending → 落到所属分类页并激活其 tab → 渲染后由 toolCard 消费。
function sendToTool(tool, args) {
  const a = args || {};
  // 媒体浏览页的「翻文件夹」不是独立工具标签，而是页面自带的目录视图：走 #/browse/{目录} 才能落到目标位置
  if (tool === 'nas_list_directory' && a.path) {
    S.pending = null;
    const tgt = '#/browse/' + encodeURIComponent(clampToRoots(a.path));
    if (location.hash === tgt) route();
    else location.hash = tgt;
    return;
  }
  S.pending = { tool, args: a };
  const nav = NAV.find((n) => (n.tools || []).includes(tool));
  const pageId = nav ? nav.id : 'workbench';
  if (nav) S.activeTab[pageId] = tool;
  S.scrollTo = 'tool-' + tool;
  const target = '#/' + pageId;
  if (location.hash === target) route();   // 同页重复跳转：hash 不变不会触发 change，手动渲染
  else location.hash = target;
}

// 路由渲染完成后把目标工具卡滚进视野（route 的 finally 里调用）
function doScrollTo() {
  if (!S.scrollTo) return;
  const id = S.scrollTo;
  S.scrollTo = null;
  requestAnimationFrame(() => {
    const node = document.getElementById(id);
    if (node) node.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

// ---------------------------------------------------------------- 全局工具搜索（Ctrl/⌘+K）
function openToolSearch() {
  const existed = document.getElementById('tool-search');
  if (existed) { existed.remove(); return; }
  const mask = el('div', 'tool-search-mask');
  mask.id = 'tool-search';
  const box = el('div', 'tool-search');
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'tool-search-input';
  input.placeholder = t('搜索功能：回车打开，Esc 关闭', '想做什么？输入「字幕」「整理」试试');
  const listBox = el('div', 'tool-search-list');
  box.append(input, listBox);
  mask.appendChild(box);
  document.body.appendChild(mask);

  let hits = [];
  let idx = 0;
  const choose = (i) => {
    const s = hits[i];
    if (!s) return;
    mask.remove();
    sendToTool(s.name, {});
  };
  const drawList = () => {
    const q = input.value.trim().toLowerCase();
    hits = Object.values(S.tools).filter((s) => !q
      || (s.name + ' ' + s.title + ' ' + s.category + ' ' + (FRIENDLY.tool[s.name] || ''))
        .toLowerCase().includes(q)).slice(0, 30);
    if (idx >= hits.length) idx = 0;
    listBox.textContent = '';
    hits.forEach((s, i) => {
      const item = el('button', 'tool-search-item' + (i === idx ? ' on' : ''));
      item.type = 'button';
      item.appendChild(el('strong', null, pick(FRIENDLY.tool, s.name, s.title)));
      item.appendChild(el('code', null, s.name));
      item.appendChild(el('span', 'badge risk ' + s.risk, pick(FRIENDLY.risk, s.risk, s.risk_label)));
      item.addEventListener('click', () => choose(i));
      listBox.appendChild(item);
    });
    if (!hits.length) listBox.appendChild(el('p', 'muted', t('没有匹配的功能', '没找到相关的')));
  };
  input.addEventListener('input', () => { idx = 0; drawList(); });
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); idx = Math.min(idx + 1, hits.length - 1); drawList(); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); idx = Math.max(idx - 1, 0); drawList(); }
    else if (ev.key === 'Enter') { ev.preventDefault(); choose(idx); }
    else if (ev.key === 'Escape') { mask.remove(); }
  });
  mask.addEventListener('click', (ev) => { if (ev.target === mask) mask.remove(); });
  drawList();
  input.focus();
}

// ---------------------------------------------------------------- 最近路径与参数记忆
const RECENT_PATHS_KEY = 'nas-recent-paths';
const SECRET_FIELD = /(password|api_key|token|secret)/i;

function loadRecentPaths() {
  try { S.recentPaths = JSON.parse(localStorage.getItem(RECENT_PATHS_KEY) || '[]') || []; } catch (_) { S.recentPaths = []; }
}

function rememberPath(p) {
  const text = String(p || '').trim();
  if (!text || text.length < 2) return;
  S.recentPaths = [text, ...S.recentPaths.filter((x) => x !== text)].slice(0, 10);
  try { localStorage.setItem(RECENT_PATHS_KEY, JSON.stringify(S.recentPaths)); } catch (_) { /* ignore */ }
}

// 最近路径补全：单文件输入框可以下拉选历史路径，少打字
function ensureRecentPaths() {
  let dl = document.getElementById('recent-paths');
  if (!dl) {
    dl = document.createElement('datalist');
    dl.id = 'recent-paths';
    document.body.appendChild(dl);
  }
  dl.textContent = '';
  for (const p of S.recentPaths) {
    const opt = document.createElement('option');
    opt.value = p;
    dl.appendChild(opt);
  }
}

function lastArgsKey(tool) { return 'nas-last-args:' + tool; }

// 只记忆非布尔、非密钥的值：布尔（尤其 dry_run=false）与密钥不复刻，避免危险/敏感复用
function rememberArgs(tool, args) {
  const keep = {};
  for (const [k, v] of Object.entries(args || {})) {
    if (typeof v === 'boolean' || SECRET_FIELD.test(k)) continue;
    if (v === null || v === undefined || v === '') continue;
    keep[k] = v;
  }
  try { localStorage.setItem(lastArgsKey(tool), JSON.stringify(keep)); } catch (_) { /* ignore */ }
}

function loadLastArgs(tool) {
  try { return JSON.parse(localStorage.getItem(lastArgsKey(tool)) || '{}') || {}; } catch (_) { return {}; }
}

// 预填优先级：S.pending（接力） > 上次填写 > defaultFor（后端默认/白名单首路径）
function applyPreseed(name, fields) {
  const hasPending = !!(S.pending && S.pending.tool === name);
  const pendingArgs = hasPending && S.pending.args && Object.keys(S.pending.args).length ? S.pending.args : null;
  if (hasPending) S.pending = null;   // 接力只消费一次
  const source = pendingArgs || loadLastArgs(name);
  if (!source || !Object.keys(source).length) return false;
  let hit = false;
  for (const f of fields) {
    const { input, param } = f;
    if (!(param.name in source)) continue;
    let v = source[param.name];
    if (param.type === 'boolean') { if (typeof v === 'boolean') { input.checked = v; hit = true; } continue; }
    if (v === null || v === undefined) continue;
    // paths 是「视频文件列表」：旧版本把它当文件夹选择，历史记忆里可能残留文件夹路径，不是文件的不回填
    if (param.name === 'paths' && Array.isArray(v)) {
      v = v.filter((p) => /\.[A-Za-z0-9]{1,5}$/.test(String(p).trim()));
      if (!v.length) continue;
    }
    const text = Array.isArray(v) ? v.join('\n') : String(v);
    // 只有接力带来的值才临时并入选项（比如上一环节选中的子目录）；
    // 历史记忆里的路径不再回填，避免下拉里出现挂载目录之外的位置
    if (hasPending && input.tagName === 'SELECT' && ![...input.options].some((o) => o.value === text)) {
      const o = document.createElement('option');
      o.value = text;
      o.textContent = text;
      input.appendChild(o);
    }
    input.value = text;
    input.dispatchEvent(new Event('input'));   // 多选条据此重画选中态
    hit = true;
  }
  return hit;
}

// ---------------------------------------------------------------- 工具表单
function defaultFor(name, param) {
  if (param.default !== undefined && param.default !== null) return param.default;
  if (name === 'dry_run') return true;
  // 所有路径类参数都预填白名单里的位置，避免手输越界被后端拒绝
  if (PATH_FIELDS.test(name)) return firstAllowedPath();
  return '';
}

function buildField(param, id, tool) {
  const field = el('div', 'field');
  const name = param.name;
  const isList = param.type === 'array' || LIST_FIELDS.test(name);
  const isPath = PATH_FIELDS.test(name);
  const isFilePath = FILE_PATH_FIELDS.test(name) || (name === 'path' && FILE_ONLY_TOOLS.has(tool));
  if (param.type === 'boolean') {
    const wrap = el('label', 'check');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = name;
    input.id = id;
    input.checked = defaultFor(name, param) === true;
    wrap.appendChild(input);
    wrap.appendChild(el('span', null, name === 'dry_run'
      ? t('仅生成计划（dry_run）', '先看计划，不动文件')
      : pick(FRIENDLY.param, name, name)));
    field.appendChild(wrap);
    return { field, input, param };
  }
  const selects = enumOptions(tool, name);
  const multi = !selects && isList && MULTI_ENUM[name] ? MULTI_ENUM[name]() : null;
  if (isList || multi) field.classList.add('wide');
  const displayName = isFilePath ? t('视频文件路径', '视频文件位置') : pick(FRIENDLY.param, name, name);
  const lab = el('label', null, displayName + (param.required ? ' *' : ''));
  lab.htmlFor = id;
  field.appendChild(lab);
  const ret = { field, input: null, param };
  let input;
  let chips = null;
  if (multi) {
    input = document.createElement('input');
    input.type = 'hidden';
    chips = buildMulti(name, multi, param, input);
  } else if (isList) {
    input = document.createElement('textarea');
    input.placeholder = '每行一个，或用逗号分隔';
    if (param.default) input.value = Array.isArray(param.default) ? param.default.join('\n') : String(param.default);
  } else if (isFilePath) {
    const wrap = el('div', 'input-with-action');
    input = document.createElement('input');
    input.type = 'text';
    input.placeholder = t('视频文件完整路径（可点击右侧浏览选择）', '视频文件位置（可点右侧选择）');
    input.setAttribute('list', 'recent-paths');
    ensureRecentPaths();
    if (param.default) input.value = String(param.default);
    wrap.appendChild(input);

    const pickBtn = el('button', 'ghost picker-btn');
    btnContent(pickBtn, 'folder', t('选择文件', '浏览'));
    pickBtn.type = 'button';
    pickBtn.title = t('在 NAS 文件目录树中选择视频文件', '在 NAS 里选择视频文件');
    pickBtn.onclick = () => {
      openFilePicker({
        initialPath: input.value.trim() || firstAllowedPath(),
        mode: 'file',
        onSelect: (selected) => {
          input.value = selected;
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
        },
      });
    };
    wrap.appendChild(pickBtn);
    input._customWrap = wrap;
  } else if (isPath && !selects) {
    if (MULTI_PATH_NAMES.has(name) && !MULTI_PATH_SKIP.has(tool)) {
      input = document.createElement('input');
      input.type = 'hidden';
      // 多个媒体库默认全选：漏勾一个盘会让扫描/补字幕/整理只跑一半。
      // 显式给了 param.default 的仍以默认值为准。
      const roots = pathOptionsFor('').map(([v]) => v);
      const preset = param.default !== undefined && param.default !== null ? param.default : roots;
      input.value = Array.isArray(preset) ? preset.join(',') : String(preset || '');
      chips = buildMulti(name, pathOptionsFor(''), param, input);

      const addDirBtn = el('button', 'ghost small', '+ ' + t('浏览添加子文件夹…', '添加文件夹…'));
      addDirBtn.type = 'button';
      addDirBtn.style.marginTop = '6px';
      addDirBtn.onclick = () => {
        openFilePicker({
          initialPath: firstAllowedPath(),
          mode: 'dir',
          onSelect: (selected) => {
            const on = new Set(lines(input.value));
            on.add(selected);
            input.value = [...on].join(',');
            input.dispatchEvent(new Event('input', { bubbles: true }));
          },
        });
      };
      chips.appendChild(addDirBtn);

      ret.multiPath = name;
      field.classList.add('wide');
    } else {
      const wrap = el('div', 'input-with-action');
      input = document.createElement('input');
      input.type = 'text';
      input.placeholder = t('输入或浏览选择目录路径…', '输入或浏览选择文件夹…');
      input.setAttribute('list', 'recent-paths');
      ensureRecentPaths();
      const def = defaultFor(name, param);
      if (def !== '' && def !== undefined) input.value = String(def);
      wrap.appendChild(input);

      const pickBtn = el('button', 'ghost picker-btn');
      btnContent(pickBtn, 'folder', t('选择目录', '浏览'));
      pickBtn.type = 'button';
      pickBtn.title = t('在 NAS 目录树中选择任意文件夹', '在 NAS 里选择文件夹');
      pickBtn.onclick = () => {
        openFilePicker({
          initialPath: input.value.trim() || firstAllowedPath(),
          mode: 'dir',
          onSelect: (selected) => {
            input.value = selected;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
          },
        });
      };
      wrap.appendChild(pickBtn);
      input._customWrap = wrap;
    }
  } else if (selects && param.type !== 'integer' && param.type !== 'number') {
    input = buildSelect(name, id, selects, param, (node) => { ret.input = node; });
  } else {
    input = document.createElement('input');
    input.type = param.type === 'integer' || param.type === 'number' ? 'number' : 'text';
    if (param.type === 'number') input.step = 'any';
    const def = defaultFor(name, param);
    if (def !== '' && def !== undefined) input.value = String(def);
  }
  input.name = name;
  input.id = id;
  if (param.required) input.required = true;
  if (input._customWrap) {
    field.appendChild(input._customWrap);
  } else {
    field.appendChild(input);
  }
  if (chips) field.appendChild(chips);
  ret.input = input;
  return ret;
}

// 单个视频文件参数：没法用「挂载目录」枚举，退回文本输入（可从媒体浏览带入 / 最近路径补全）
const FILE_PATH_FIELDS = /(^|_)video_path$/;
const FILE_ONLY_TOOLS = new Set(['media_quality_analyze', 'nas_check_subtitle']);
// 已挂载目录（后端 allowed_paths_expanded）就是可选项全集；接力带来的子目录临时并入，保证能选中
function pathOptionsFor(current) {
  const cfg = S.config || {};
  const roots = cfg.allowed_paths_expanded && cfg.allowed_paths_expanded.length
    ? cfg.allowed_paths_expanded
    : (cfg.allowed_paths || []);
  const opts = roots.map((p) => [p, p]);
  const cur = String(current || '');
  if (cur && !opts.some(([v]) => v === cur)) opts.unshift([cur, cur]);
  if (!opts.length) {
    const fallback = firstAllowedPath();
    opts.push([fallback, fallback]);
  }
  return opts;
}

// 多选：一组小按钮，选中项写回隐藏 input（用逗号拼接，提交时按列表解析）
function buildMulti(name, options, param, input) {
  const chips = el('div', 'multi-chips');
  const buttons = new Map();
  const add = (value, labelText) => {
    if (buttons.has(value)) return;
    const b = el('button', 'chip-toggle', labelText || value);
    b.type = 'button';
    b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', () => {
      const on = new Set(lines(input.value));
      if (on.has(value)) on.delete(value);
      else on.add(value);
      input.value = [...on].join(',');
      paint();
    });
    buttons.set(value, b);
    chips.appendChild(b);
  };
  const paint = () => {
    // 值里出现候选之外的路径（接力带入的子目录）时补一个芯片，避免「选中了却看不见」
    for (const v of lines(input.value)) if (!buttons.has(v)) add(v, v);
    const on = new Set(lines(input.value));
    for (const [value, btn] of buttons) {
      btn.setAttribute('aria-pressed', on.has(value) ? 'true' : 'false');
    }
  };
  for (const [value, labelText] of options) add(value, t(labelText, labelText));
  const preset = param.default;
  if (Array.isArray(preset) && preset.length) input.value = preset.join(',');
  input.addEventListener('input', paint);
  paint();
  return chips;
}

function buildSelect(name, id, options, param, setInput, allowCustom = true) {
  const select = document.createElement('select');
  const def = String(defaultFor(name, param) ?? '');
  const opts = def && !options.some(([v]) => v === def) ? [...options, [def, def]] : options;
  for (const [value, textLabel] of opts) {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = value === '' || textLabel === value
      ? t(textLabel, textLabel)
      : t(`${textLabel}（${value}）`, textLabel);
    select.appendChild(o);
  }
  if (!allowCustom) {
    // 没有可选自定义项时，必定落在某个真实选项上，避免出现空白选择
    select.value = opts.some(([v]) => v === def) ? def : String((opts[0] && opts[0][0]) || '');
    return select;
  }
  const custom = document.createElement('option');
  custom.value = '__custom__';
  custom.textContent = t('其他（手动输入）', '其他…');
  select.appendChild(custom);
  select.value = opts.some(([v]) => v === def) || !def ? def : '__custom__';
  select.addEventListener('change', () => {
    if (select.value !== '__custom__') return;
    const text = document.createElement('input');
    text.type = 'text';
    text.name = name;
    text.id = id;
    if (param.required) text.required = true;
    if (def) text.value = def;
    text.placeholder = t('手动输入一个值', '手动输入一个值');
    select.replaceWith(text);
    text.focus();
    setInput(text);
  });
  return select;
}

// 一句话人话说明：优先用前端文案，缺省时退回后端 docstring 的第一行
function toolSummary(name, spec) {
  if (FRIENDLY.desc[name]) return FRIENDLY.desc[name];
  const first = String((spec && spec.description) || '').split('\n')[0].trim();
  return first || '';
}

// 技术信息：工具代号、后端 docstring、逐参数说明全部收进折叠项，卡片首屏只留人话
function toolTechInfo(name, spec) {
  const tech = el('details', 'tech-detail');
  tech.appendChild(el('summary', null, '技术信息'));
  tech.appendChild(el('code', null, name));
  if (spec.description) {
    const doc = el('pre', 'tech-doc', spec.description.trim());
    tech.appendChild(doc);
  }
  const documented = (spec.params || []).filter((p) => p.description);
  if (documented.length) {
    const box = el('div', 'tech-params');
    for (const p of documented) {
      const row = el('div', 'tech-param');
      row.appendChild(el('code', null, p.name));
      row.appendChild(el('span', null, p.description));
      box.appendChild(row);
    }
    tech.appendChild(box);
  }
  return tech;
}

function toolCard(name) {
  const spec = S.tools[name];
  const card = el('article', 'card tool-card');
  const head = el('div', 'card-head');
  const left = el('div');
  const titleRow = el('div', 'result-head');
  titleRow.appendChild(el('h2', null, pick(FRIENDLY.tool, name, spec.title)));
  titleRow.appendChild(el('span', 'badge risk ' + spec.risk, pick(FRIENDLY.risk, spec.risk, spec.risk_label)));
  if (spec.long_running) titleRow.appendChild(el('span', 'badge warn', t('后台任务', '要多等一会儿')));
  left.appendChild(titleRow);
  const desc = el('div', 'desc', toolSummary(name, spec));
  if (desc.textContent) left.appendChild(desc);
  left.appendChild(toolTechInfo(name, spec));
  head.appendChild(left);
  card.appendChild(head);

  const body = el('div', 'card-body');
  // 写操作门预警示：开关关闭时（=== false 才触发，配置未加载时不误报）
  const gateCfg = S.config || {};
  const offGates = (TOOL_GATES[name] || []).filter((g) => gateCfg[g] === false);
  if (offGates.length) {
    body.appendChild(el('div', 'notice',
      t(`执行类操作需要在「配置」页开启：${offGates.join('、')}（当前关闭）；仅 dry_run 预览不受影响。`,
        `真正动手前，要先到「配置」页打开「${offGates.map((g) => GATE_LABELS[g]).join('」和「')}」；只看计划不受影响。`)));
  }
  card.id = 'tool-' + name;   // 深链/接力后按 id 滚动定位
  const form = el('form', 'tool-form');
  const fields = (spec.params || []).map((p, i) => buildField(p, `${name}-${p.name}-${i}`, name));
  const preseeded = applyPreseed(name, fields);
  if (preseeded) form.classList.add('preseeded');

  // 渐进披露：只把「有默认值的数值/开关类」次要参数收进「更多选项」，
  // 枚举类的关键选择（mode / lang / 类别）留在首屏，避免把有意义的选择藏起来
  const advanced = (p) => {
    if (p.name === 'dry_run' || p.required) return false;
    if (p.default === undefined || p.default === null) return false;
    if (p.name === 'preexisting_empty_scope') return true;
    return p.type === 'boolean' || p.type === 'integer' || p.type === 'number';
  };
  const basic = fields.filter((f) => !advanced(f.param));
  const extra = fields.filter((f) => advanced(f.param));
  for (const f of basic) form.appendChild(f.field);
  if (extra.length) {
    const more = el('details', 'more-options');
    // 只有「更多选项」里真的带了非默认值才展开，否则首屏保持清爽
    more.open = extra.some((f) => {
      const def = defaultFor(f.param.name, f.param);
      return f.param.type === 'boolean'
        ? f.input.checked !== (def === true)
        : String(f.input.value || '') !== String(def === undefined || def === null ? '' : def);
    });
    more.appendChild(el('summary', null, t(`更多选项（${extra.length}）`, `还能再调调（${extra.length} 项）`)));
    const box = el('div', 'more-body');
    for (const f of extra) box.appendChild(f.field);
    more.appendChild(box);
    form.appendChild(more);
  }

  const actions = el('div', 'form-actions');
  const submit = document.createElement('button');
  submit.type = 'submit';
  submit.className = spec.risk === 'delete' ? 'danger' : 'primary';
  submit.textContent = spec.long_running ? t('提交后台任务', '开始（后台慢慢做）') : t('执行', '开始');
  const reset = el('button', 'ghost', t('重置', '清空重来'));
  reset.type = 'button';
  reset.addEventListener('click', () => {
    for (const f of fields) {
      const { input, param } = f;
      const def = defaultFor(param.name, param);
      if (param.type === 'boolean') { input.checked = def === true; continue; }
      let value = def;
      if (Array.isArray(value)) value = value.join('\n');
      input.value = value === undefined || value === null ? '' : String(value);
      input.dispatchEvent(new Event('input'));
    }
    out.textContent = '';
  });
  // 动作区遵循惯例：说明在左，次要动作其次，主动作最右
  if (spec.long_running) actions.appendChild(el('span', 'note', t('耗时任务在后台执行，可切换页面，稍后在工具台查看结果。', '这件事要多花点时间，可以先去忙别的，稍后在「工具台 → 后台任务」看结果。')));
  actions.appendChild(reset);
  actions.appendChild(submit);
  form.appendChild(actions);
  body.appendChild(form);

  const out = el('div', 'out');
  body.appendChild(out);
  card.appendChild(body);

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const args = {};
    for (const f of fields) {
      const { input, param } = f;
      if (param.type === 'boolean') { args[param.name] = input.checked; continue; }
      const raw = input.value.trim();
      if (raw === '') continue;
      if (param.type === 'array' || LIST_FIELDS.test(param.name)) args[param.name] = lines(raw);
      else if (param.type === 'integer') args[param.name] = parseInt(raw, 10);
      else if (param.type === 'number') args[param.name] = Number(raw);
      else args[param.name] = raw;
    }
    // 目录多选：勾了几个文件夹，就逐个执行几次
    const batchField = fields.find((f) => f.multiPath);
    const batchPaths = batchField ? lines(batchField.input.value) : [];
    if (batchField && batchField.param.required && !batchPaths.length) {
      toast(t('至少选择一个文件夹。', '先勾一个文件夹吧'), true);
      return;
    }
    // 后端要求 path 与 paths 二选一：
    // 单文件工具填了 path 就以 path 为准（忽略列表），否则显式文件列表优先
    const singleFile = fields.some((f) => (FILE_PATH_FIELDS.test(f.param.name)
      || (f.param.name === 'path' && FILE_ONLY_TOOLS.has(name))) && f.input.value.trim());
    if (singleFile) delete args.paths;
    const explicitList = Array.isArray(args.paths) && args.paths.length > 0;
    if (explicitList) delete args.path;
    else if (batchPaths.length) args[batchField.param.name] = batchPaths[0];
    const batchCount = explicitList ? 0 : batchPaths.length;
    if (spec.risk !== 'read' && args.dry_run === false
      && !confirm(batchCount > 1
        ? t(`即将对 ${batchCount} 个文件夹执行「${spec.title}」的非 dry_run 操作，可能修改/删除 NAS 文件。确认继续？`, `接下来会真实改动这 ${batchCount} 个文件夹里的文件（不只是预览）。确定继续吗？`)
        : t(`即将执行「${spec.title}」的非 dry_run 操作，可能修改/删除 NAS 文件。确认继续？`, '接下来会真实修改 NAS 上的文件（不只是预览）。确定继续吗？'))) return;
    rememberArgs(name, args);
    if (batchPaths.length) {
      for (const p of batchPaths) rememberPath(p);
    } else {
      for (const f of fields) {
        if (PATH_FIELDS.test(f.param.name)) rememberPath(f.input.value);
      }
    }
    submit.disabled = true;
    submit.setAttribute('aria-busy', 'true');
    out.textContent = '';
    out.appendChild(spinner(spec.long_running ? t('已提交，后台执行中…', '已提交，正在后台处理…') : t('执行中…', '正在处理…')));
    out.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    try {
      if (batchCount > 1) {
        await runBatch(name, spec, args, batchField.param.name, batchPaths, out);
        return;
      }
      const ctx = { tool: name, args };
      if (spec.long_running) {
        const created = await api('/api/jobs', { method: 'POST', body: { tool: name, args } });
        pollJobsIndicator();
        const job = await pollJob(created.job.id, out);
        if (job.status === 'succeeded' && job.result) renderResult(job.result, out, ctx);
        else renderResult({ ok: false, error: job.error || { code: job.status, message: '任务未成功' } }, out, ctx);
      } else {
        const result = await api('/api/tools/' + encodeURIComponent(name), { method: 'POST', body: { args } });
        renderResult(result, out, ctx);
      }
    } catch (err) {
      out.textContent = '';
      out.appendChild(errorBox({ code: err.code || 'request_failed', message: err.message }, { retry: () => form.requestSubmit() }));
    } finally {
      submit.disabled = false;
      submit.removeAttribute('aria-busy');
    }
  });
  return card;
}

async function pollJob(id, out) {
  let job = null;
  const started = Date.now();
  for (let i = 0; i < 900; i += 1) {
    const data = await api('/api/jobs/' + encodeURIComponent(id) + '?wait=20');
    job = data.job;
    if (JOB_TERMINAL.has(job.status)) return job;
    out.textContent = '';
    const waited = Math.round((Date.now() - started) / 1000);
    const p = job.progress || {};
    let stageText = '';
    if (p.label || p.stage) {
      stageText = p.label || p.stage;
      if (p.total) stageText += ` ${p.done || 0}/${p.total}`;
      else if (p.done) stageText += ` 已处理 ${p.done}`;
      stageText += ' · ';
    }
    out.appendChild(spinner(IS_PRO()
      ? `任务 ${job.id} · ${job.status} · ${stageText}已等待 ${waited}s`
      : `正在处理 · ${FRIENDLY.job[job.status] || job.status} · ${stageText}已等待 ${waited} 秒`));
    if (Date.now() - started > 1000 * 60 * 60) break;
  }
  return job || { status: 'failed', error: { code: 'timeout', message: '等待任务结果超时' } };
}

// 终态：与后端 TERMINAL_STATUSES 对齐（interrupted 是应用重启导致的独立终态）
const JOB_TERMINAL = new Set(['succeeded', 'failed', 'canceled', 'interrupted']);

// 部分结果以任务结果里的 canceled/partial 标记为准；列表接口不内联 result 时
// 用后端聚合出的扁平 partial 字段，不用状态推断
function jobPartial(job) {
  if (!job) return false;
  if (job.partial === true) return true;
  return !!(job.result && (job.result.canceled === true || job.result.partial === true));
}

// 同一个工具勾了多个文件夹时：逐个子目录执行，结果按文件夹分段展示
// （后端 path 参数只收一个，所以在前端做批量，不改后端签名）
async function runBatch(name, spec, args, pathName, paths, out) {
  out.textContent = '';
  let okCount = 0;
  for (let i = 0; i < paths.length; i += 1) {
    const one = Object.assign({}, args, { [pathName]: paths[i] });
    const item = el('div', 'batch-item');
    const head = el('div', 'result-head');
    head.appendChild(el('span', 'batch-path mono', paths[i]));
    head.appendChild(el('span', 'badge', `${i + 1} / ${paths.length}`));
    item.appendChild(head);
    const body = el('div', 'batch-body');
    item.appendChild(body);
    out.appendChild(item);
    body.appendChild(spinner(spec.long_running ? t('已提交，后台执行中…', '正在后台处理…') : t('执行中…', '正在处理…')));
    try {
      let result;
      if (spec.long_running) {
        const created = await api('/api/jobs', { method: 'POST', body: { tool: name, args: one } });
        pollJobsIndicator();
        const job = await pollJob(created.job.id, body);
        result = JOB_TERMINAL.has(job.status) && job.result
          ? job.result
          : { ok: false, error: job.error || { code: job.status, message: '任务未成功' } };
      } else {
        result = await api('/api/tools/' + encodeURIComponent(name), { method: 'POST', body: { args: one } });
      }
      const ok = result && result.ok !== false;
      if (ok) okCount += 1;
      head.appendChild(el('span', 'badge ' + (ok ? 'ok' : 'err'), ok ? t('完成', '搞定') : t('失败', '没成功')));
      renderResult(result, body, { tool: name, args: one });
    } catch (err) {
      head.appendChild(el('span', 'badge err', t('失败', '没成功')));
      body.textContent = '';
      body.appendChild(errorBox({ code: err.code || 'request_failed', message: err.message }));
    }
  }
  out.appendChild(el('p', 'muted', t(
    `批量完成：${okCount} / ${paths.length} 个文件夹成功。`,
    `${okCount} / ${paths.length} 个文件夹搞定。`)));
}

// ---------------------------------------------------------------- 结果渲染
function renderResult(result, out, ctx = null) {
  out.textContent = '';
  if (!result) {
    out.appendChild(emptyState({
      title: t('没有返回结果', '这次没有拿到结果'),
      hint: t('工具没有返回内容，可以重试一次。', '工具没有返回内容，可以再试一次。'),
      action: { label: t('重试', '再试一次'), onClick: () => route() },
    }));
    return;
  }
  if (result.ok === false) {
    const err = typeof result.error === 'string'
      ? { code: result.error, message: result.message || result.error }
      : (result.error || { code: 'failed', message: result.message || '工具返回失败' });
    out.appendChild(errorBox(err));
    out.appendChild(rawDetails(result));
    return;
  }
  const head = el('div', 'result-head');
  head.appendChild(el('span', 'badge ok', t('完成', '搞定')));
  if (result.duration_ms != null) head.appendChild(el('span', 'badge', (Number(result.duration_ms) / 1000).toFixed(2) + ' s'));
  if (result.job) head.appendChild(el('span', 'badge', t('任务 ' + (result.job.id || ''), '来自后台任务')));
  // 友好版不常驻复制按钮；原始数据在最后的「查看原始数据」折叠里
  if (IS_PRO()) head.appendChild(copyButton(() => JSON.stringify(result, null, 2), t('复制结果 JSON', '复制原始数据')));
  out.appendChild(head);

  if (result.summary && typeof result.summary === 'object') {
    const chips = el('div', 'summary');
    for (const [key, value] of Object.entries(result.summary)) {
      if (value === null || value === undefined) continue;
      if (typeof value === 'object') continue;
      const chip = el('span', 'chip');
      chip.appendChild(el('b', null, label(key)));
      chip.appendChild(document.createTextNode(' ' + fmtCell(value, key)));
      chips.appendChild(chip);
    }
    if (chips.childNodes.length) out.appendChild(chips);
  }

  // 结果接力：本次结果可直接带入的下一步
  if (ctx && HANDOFFS_RESULT[ctx.tool]) {
    let items = [];
    try { items = HANDOFFS_RESULT[ctx.tool](result, ctx.args || {}) || []; } catch (_) { items = []; }
    if (items.length) out.appendChild(handoffBar(items));
  }

  const arrays = [];
  const scalars = [];
  const emptyLists = [];
  // 这些数组为空就是「这件事没有结果」，要用带行动按钮的空状态，而不是一行「（没有内容）」
  const MEANINGFUL_EMPTY = new Set(['results', 'candidates', 'entries', 'shows', 'groups', 'suspects',
    'duplicate_groups', 'items', 'files', 'plan', 'plans', 'executed', 'failed_items', 'site_attempts']);
  for (const [key, value] of Object.entries(result)) {
    if (['ok', 'summary', 'duration_ms', 'job'].includes(key)) continue;
    if (Array.isArray(value)) {
      if (!value.length) {
        if (MEANINGFUL_EMPTY.has(key)) emptyLists.push(key);
        else scalars.push([key, value]);
      } else if (typeof value[0] === 'object' && value[0] !== null) {
        arrays.push([key, value]);
      } else {
        scalars.push([key, value]);
      }
    } else if (value && typeof value === 'object') {
      if (key === 'error') continue;
      scalars.push([key, value]);
    } else {
      scalars.push([key, value]);
    }
  }

  if (scalars.length) out.appendChild(renderKv(scalars));
  for (const key of emptyLists) {
    const searching = /results|candidates|site_attempts/.test(key);
    out.appendChild(emptyState({
      title: searching ? t('没有找到结果。', '没有找到结果') : t('这里现在没有内容。', '这里现在没有内容'),
      hint: searching
        ? t('换个关键词，或调整筛选条件再试一次。', '换个词，或者放宽条件再试一次。')
        : t('换个位置或条件再试一次。', '换个位置或条件再试一次。'),
      action: emptyActionForTool(ctx && ctx.tool),
    }));
  }
  for (const [key, arr] of arrays) {
    const opts = { tool: ctx && ctx.tool };
    if (key === 'results') {
      // 按行结构分派：只有真正的资源搜索形状才用 5 列 + 来源筛选；
      // 画质、产地、磁力、网盘等结果各用匹配自己结构的列。
      const kind = resultKind(ctx && ctx.tool, arr);
      opts.kind = kind;
      const q = (ctx && ctx.args && ctx.args.query) || result.query || '';
      opts.query = q;
      if (kind === 'search') {
        // 只放 5 个能决定「要不要下」的列，其余字段（磁力/网盘地址、
        // 置信度、来源标记、告警）全部进「详情」抽屉。
        opts.filterable = true;
        opts.note = q
          ? t(`相关性 = 片名/年份/季集匹配 + 做种数/体积合理性；网盘按实时探测`, `相关性依据：片名、年份、季集对不对得上，以及做种数和体积是否合理；网盘以实时探测为准`)
          : '';
      }
      // 资源搜索结果（含磁力/网盘/直链）统一分页，不静默截断
      if (['search', 'links', 'magnet', 'pan'].includes(kind)) {
        opts.paged = true;
        opts.pageSize = 40;
      }
    }
    // 媒体列表 / 画质结果：固定列宽自适应容器，窄宽度不出横向滚动条；
    // 被收起的字段（音轨明细、完整路径、mtime、位深/码率等）放进行内展开。
    if (key === 'videos' || (key === 'results' && opts.kind === 'quality')) {
      opts.fit = true;
      opts.detail = (src) => mediaDetail(src, key === 'videos' ? 'videos' : 'quality');
    }
    const hinted = arrayColumns(key, arr, opts.kind);
    if (hinted) opts.columns = hinted;
    const rowFn = ctx && HANDOFFS_ROWS[ctx.tool] && HANDOFFS_ROWS[ctx.tool][key];
    if (rowFn) {
      opts.handoff = (src, row) => {
        try { return rowFn(src, row, ctx) || []; } catch (_) { return []; }
      };
    }
    out.appendChild(renderTable(key, arr, opts));
  }

  if (result.error) {
    const err = typeof result.error === 'string'
      ? { code: result.error, message: result.message || result.error }
      : result.error;
    out.appendChild(errorBox(err));
  }
  out.appendChild(rawDetails(result));
}

// 结果头的「带着结果继续」按钮条
function handoffBar(items) {
  const bar = el('div', 'handoff');
  bar.appendChild(el('span', 'muted', t('带着结果继续：', '接下来可以：')));
  for (const item of items) {
    const btn = el('button', 'ghost small', item.label);
    btn.type = 'button';
    btn.addEventListener('click', () => sendToTool(item.tool, item.args || {}));
    bar.appendChild(btn);
  }
  return bar;
}

function renderKv(entries) {
  const wrap = el('div', 'kv');
  for (const [key, value] of entries) {
    const row = el('div', 'row');
    row.appendChild(el('span', null, label(key)));
    let text;
    if (Array.isArray(value)) {
      if (!value.length) text = t('（空）', '（没有内容）');
      else if (typeof value[0] === 'object') text = value.length + ' 项';
      else text = value.join('、');
    } else if (value && typeof value === 'object') {
      text = objectSummary(value);
    } else {
      text = fmtCell(value, key);
    }
    row.appendChild(el('span', null, text));
    wrap.appendChild(row);
  }
  return wrap;
}

function objectSummary(value) {
  const entries = Object.entries(value);
  if (!entries.length) return t('{}（空对象）', '（没有内容）');
  const simple = entries.every(([, v]) => v == null || ['string', 'number', 'boolean'].includes(typeof v));
  if (simple) {
    return entries.map(([k, v]) => `${label(k)} ${fmtCell(v, k)}`).join('，');
  }
  return t('内容较复杂，见下方「技术详情（原始数据）」', '内容较复杂，展开下面的「技术详情」可看原始数据');
}

// 标题命中：按空白和中英文边界切词，避免把「流人s06」当成一个整词，从而漏判
function queryTokens(q) {
  const words = String(q || '').toLowerCase().split(/[\s,，、;；|/]+/);
  const out = [];
  for (const w of words) {
    const parts = w.match(/[\u4e00-\u9fa5]+|[a-z0-9]+/g) || [w];
    for (const p of parts) {
      if (p.length >= 2 || /\d/.test(p)) out.push(p);
    }
  }
  return out.slice(0, 8);
}

function titleHit(query, title) {
  const tokens = queryTokens(query);
  if (!tokens.length) return null;
  const hay = String(title || '').toLowerCase();
  const hit = tokens.filter((tk) => hay.includes(tk));
  return { hit, total: tokens.length };
}

// 相关性跟随后端证据口径：
//   high/partial/suspicious/mismatch/unverified 分别对应「高相关 / 部分命中 /
//   元数据可疑 / 不匹配 / 无法判定」；verified 只在网盘实时探测为真时出现。
// 老结果没有 confidence 时，才退回「标题是否命中」的客户端判断。
function relevanceEvidence(text) {
  return String(text || '')
    .replace(/[；;]\s*磁力未校验实际内容[^；;]*$/, '')
    .replace(/^标题完整匹配（(?:片名|别名)「[^」]+」）/, '')
    .replace(/^标题不匹配[：:]?/, '')
    .replace(/^部分命中，?缺少/, '缺少')
    .replace(/^[；;、：:\s]+/, '')
    .trim();
}

function relevanceText(row, query) {
  const conf = String(row.confidence || '');
  const reason = relevanceEvidence(row.warning);
  if (row.mismatch || conf === 'mismatch') {
    return '不匹配' + (reason ? '：' + reason : '');
  }
  if (conf === 'high') {
    return row.verified ? '高 · 已验证' : '高（仅标题匹配）';
  }
  if (conf === 'suspicious') {
    return '可疑' + (reason ? '：' + reason : '，标题匹配但元数据异常');
  }
  if (conf === 'partial') {
    return '部分命中' + (reason ? '：' + reason : '');
  }
  if (conf === 'unverified') {
    return '无法判定' + (reason ? '：' + reason : '');
  }
  if (conf === 'medium' || conf === 'low') {
    return ({ medium: '中', low: '低' }[conf]) + (row.verified ? ' · 已验证' : '');
  }
  const m = titleHit(query, row.title);
  if (m) {
    if (m.hit.length === m.total) return '命中 ' + m.total + '/' + m.total;
    if (m.hit.length) return '部分命中 ' + m.hit.length + '/' + m.total + '（缺「' + queryTokens(query).filter((tk) => !m.hit.includes(tk)).join('、') + '」）';
    return '未命中标题';
  }
  return '';
}

// ---------------------------------------------------------------- 结果结构识别
// 结果渲染按「行结构」分派，绝不假设所有工具都返回资源搜索表。
// 工具提示只用于消除结构相近的歧义（如裸 url 是影片页还是网盘页）；
// 结构不符时一律退回通用渲染。
const RESULT_KIND_BY_TOOL = {
  media_search: 'search',
  jackett_search: 'search',
  seedhub_search: 'links',
  seedhub_magnets: 'magnet',
  seedhub_pans: 'pan',
  dysbk_search: 'links',
  media_quality_analyze: 'quality',
  nas_classify_region: 'region',
};

function ratioOf(rows, key) {
  if (!rows.length) return 0;
  const hit = rows.filter((r) => r && typeof r === 'object'
    && r[key] !== undefined && r[key] !== null && r[key] !== '').length;
  return hit / rows.length;
}

function detectResultKind(arr) {
  const rows = arr.filter((r) => r && typeof r === 'object' && !Array.isArray(r));
  if (!rows.length || rows.length !== arr.length) return 'generic';
  const cov = (k) => ratioOf(rows, k);
  if (cov('title') < 0.5) return 'generic';
  if (cov('quality_score') >= 0.5 && cov('resolution') >= 0.5) return 'quality';
  if (cov('region') >= 0.5) return 'region';
  if (cov('magnet') >= 0.5 && cov('source') < 0.5 && cov('seeders') < 0.5) return 'magnet';
  if ((cov('pan_url') >= 0.5 || cov('host') >= 0.5) && cov('magnet') < 0.5) return 'pan';
  if (cov('source') >= 0.5 || cov('seeders') >= 0.3) return 'search';
  return 'generic';
}

function resultKind(tool, arr) {
  const rows = arr.filter((r) => r && typeof r === 'object' && !Array.isArray(r));
  if (!rows.length || rows.length !== arr.length) return 'generic';
  const hint = RESULT_KIND_BY_TOOL[tool];
  if (hint) {
    if (hint === 'search' && ratioOf(rows, 'title') < 0.5) return 'generic';
    if (hint === 'quality' && ratioOf(rows, 'quality_score') < 0.5) return 'generic';
    if (hint === 'region' && ratioOf(rows, 'region') < 0.5) return 'generic';
    return hint;
  }
  return detectResultKind(arr);
}

// 指定列：只列真正有值的列，缺字段时由 renderTable 自动剔除
function arrayColumns(key, arr, kind) {
  if (key === 'results') {
    if (kind === 'search') return ['标题', '来源', '大小', '做种', '相关性'];
    if (kind === 'quality') return ['名称', '分辨率', '编码', '画质', '音轨', '大小', '评级'];
    if (kind === 'region') return ['名称', '产地', '置信度', '音轨语言', '探测'];
    if (kind === 'magnet') return ['标题', '大小', '磁力', '更新时间'];
    if (kind === 'pan') return ['标题', '网盘', '链接', '核验', '更新时间', '提示'];
    if (kind === 'links') return ['标题', 'ID', '链接'];
    return null;
  }
  if (key === 'videos') return ['名称', '分辨率', '编码', '画质', '音轨', '大小', '中字'];
  if (key === 'candidates') {
    const sample = arr.find((x) => x && typeof x === 'object') || {};
    if ('canonical_title' in sample || 'tmdb_id' in sample) {
      return ['标题', '原名', '年份', '类型', 'TMDB ID', '语言', '产地'];
    }
    return ['标题', '站点', '评分', '格式', '语言', '整季包', '发布名'];
  }
  if (key === 'matches') {
    const sample = arr.find((x) => x && typeof x === 'object') || {};
    if ('parsed' in sample) return ['名称', '标题', '年份', '季', '集', '分辨率', '大小', '路径'];
    return ['名称', '类型', '类别', '扩展名', '大小', '修改时间', '相对路径'];
  }
  if (key === 'shows') return ['标题', '年份', '季', '应有集数', '实有集数', '缺失集', '说明'];
  if (key === 'groups' || key === 'suspects' || key === 'duplicate_groups') return ['标题', '年份', '类型', '成员数', '最佳版本', '时长矛盾'];
  if (key === 'multi_root_shows') return ['标题', '类型', 'TMDB ID', '根数', '剧目根'];
  if (key === 'seasons') return ['剧名', '季', '总集数', '缺失', '策略', '计划', '已补', '失败', '跳过'];
  if (key === 'plan') return ['动作', '类型', '级别', '源', '目标', '原因', '置信度'];
  if (key === 'plans') return ['视频', '目标文件', '策略', '已有字幕', '外挂字幕数', '新增音轨数'];
  if (key === 'executed') return ['视频', '目标文件', '策略', '已有字幕', '外挂字幕数', '新增音轨数'];
  if (key === 'errors') {
    const sample = arr.find((x) => x && typeof x === 'object') || {};
    if ('video_path' in sample) return ['视频', '目标文件', '错误'];
    if ('site' in sample) return ['站点', '错误'];
    if ('reason' in sample && !('error' in sample)) return ['路径', '原因'];
    return ['路径', '错误'];
  }
  if (key === 'failed_items') return ['类型', '路径', '恢复到', '状态', '原因'];
  if (key === 'site_attempts') return ['站点', '状态', '结果数', '最高分', '说明'];
  if (key === 'items') {
    const sample = arr.find((x) => x && typeof x === 'object') || {};
    if ('classification' in sample || 'cleanup_allowed' in sample) {
      return ['路径', '大小', '分类', '动作', '可清理', '保护原因', '匹配规则'];
    }
    if ('restore_to' in sample) return ['类型', '路径', '恢复到', '状态', '原因'];
    if ('container' in sample || 'has_video_stream' in sample) {
      return ['名称', '路径', '状态', '探测', '原因', '容器', '时长', '大小'];
    }
    return null;
  }
  return null;
}

const REGION_TEXT = { domestic: '国产', non_domestic: '非国产', unknown: '未知' };
const QUALITY_TIER_TEXT = { excellent: '优秀', good: '良好', fair: '一般', poor: '较差', bad: '差', unknown: '未知' };
const PLAN_ACTION_TEXT = {
  move: '移动', skip: '跳过', identical: '已一致', duplicate: '重复',
  conflict: '冲突', blocked: '受限', cleanup_dir: '清理空目录',
};
const CLEANUP_ACTION_TEXT = { delete: '删除', review: '待确认', keep: '保留' };
const CLEANUP_PROTECT_TEXT = {
  not_media_junk: '不是垃圾文件',
  protected_by_skip_pattern: '命中排除规则',
  protected_by_special_dir_names: '位于特殊目录（样本/花絮）',
  active_download_grace: '可能正在下载',
  file_in_use: '文件被占用',
  file_status_unknown: '无法确认是否占用，默认保护',
};
const FILL_STRATEGY_TEXT = { pack: '整季包', individual: '逐集', skipped: '跳过' };
const MISSING_REASON_TEXT = {
  expected_episode_set_unavailable: '未启用 TMDB/TVmaze，无法判断应有多少集',
};
const CLEANUP_CLASS_TEXT = { media: '媒体', media_junk: '垃圾文件', personal_video: '个人视频', unknown: '未知' };
const INTEGRITY_STATUS_TEXT = { healthy: '健康', corrupted: '损坏', warning: '可疑', unknown: '未探明', incomplete: '不完整' };
const ROLLBACK_STATUS_TEXT = {
  rollback_ready: '可回滚', rolled_back: '已回滚', partially_rolled_back: '部分回滚',
  rollback_conflict: '回滚冲突', rollback_unsupported: '不支持回滚', permission_denied: '没有权限',
  not_found: '未找到', failed: '失败', nothing_to_rollback: '没有可回滚内容',
};
const SITE_ATTEMPT_STATUS_TEXT = {
  ok: '有结果', no_results: '确认无结果', error: '出错', stopped_early: '已提前停止',
};

function fileNameOf(path) {
  const text = String(path || '');
  const idx = text.lastIndexOf('/');
  return idx >= 0 ? text.slice(idx + 1) : text;
}

function shortSize(value) {
  if (value === undefined || value === null || value === '') return '';
  // 后端有的结果给字节数，有的给已格式化字符串；fmtSize 对两者都能处理
  return fmtSize(value);
}

// ---------------------------------------------------------------- 媒体规格标签
// ffprobe 维度 → 紧凑标签（DV P8.1 / HDR10+ / Atmos·TrueHD 7.1 / DTS:X）。
// 新结果带结构字段；老任务结果只有 hdr_format / dv_profile / has_atmos，按旧形状回退。
const CODEC_TEXT = {
  hevc: 'HEVC', h265: 'HEVC', h264: 'AVC', avc: 'AVC', av1: 'AV1', vp9: 'VP9',
  mpeg4: 'MPEG-4', xvid: 'XviD', divx: 'XviD', vc1: 'VC-1',
  truehd: 'TrueHD', eac3: 'DD+', ac3: 'AC-3', dts: 'DTS', aac: 'AAC',
  flac: 'FLAC', opus: 'Opus', vorbis: 'Vorbis', mp3: 'MP3',
};

function codecLabel(raw, bitDepth) {
  const text = String(raw || '');
  const base = CODEC_TEXT[text.toLowerCase()] || text.toUpperCase() || '';
  const bit = Number(bitDepth) > 0 ? Number(bitDepth) + 'bit' : '';
  return [base, bit].filter(Boolean).join(' ');
}

function fmtBitRate(bps) {
  const n = Number(bps);
  if (!isFinite(n) || n <= 0) return '';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 10e6 ? 0 : 1) + ' Mbps';
  return Math.round(n / 1000) + ' kbps';
}

function fmtSampleRate(hz) {
  const n = Number(hz);
  return isFinite(n) && n > 0 ? (n / 1000).toFixed(n % 1000 ? 1 : 0) + ' kHz' : '';
}

function dvShort(video) {
  if (!video) return '';
  const isDv = !!video.dolby_vision || video.dv_profile != null
    || /dolby vision/i.test(String(video.hdr_format || video.hdr || ''));
  if (!isDv) return '';
  const profile = video.dv_profile_label
    || (video.dv_profile != null ? 'Profile ' + video.dv_profile : '');
  return profile ? 'DV ' + profile.replace(/^Profile\s*/i, 'P') : 'DV';
}

function rangeLabel(video, probed) {
  if (!video) return '';
  const legacy = typeof video.hdr === 'string' ? video.hdr : '';
  const legacyFormat = String(video.hdr_format || legacy || '');
  const tags = [];
  const push = (tag) => { if (tag && !tags.includes(tag)) tags.push(tag); };
  if (Array.isArray(video.hdr_types) && video.hdr_types.length) {
    for (const item of video.hdr_types) push(item === 'Dolby Vision' ? dvShort(video) : item);
  } else {
    push(dvShort(video));
    if (video.hdr10_plus) push('HDR10+');
    if (video.hdr10) push('HDR10');
    if (video.hlg) push('HLG');
    if (!tags.length && legacyFormat) {
      for (const part of legacyFormat.split(/\s+\+\s+/)) {
        if (/dolby vision/i.test(part)) push(dvShort(video) || 'DV');
        else if (/hdr10\+/i.test(part)) push('HDR10+');
        else if (/hdr10/i.test(part)) push('HDR10');
        else if (/hlg/i.test(part)) push('HLG');
        else push(part);
      }
    }
    if (!tags.length && video.hdr === true) push(legacyFormat || 'HDR');
  }
  if (!tags.length && (video.sdr === true || probed)) push('SDR');
  return tags.join(' · ');
}

function channelLayout(a) {
  const text = String(a.channel_layout_label || a.channel_layout || '')
    .replace(/\(.*?\)/g, '').trim();
  if (text) return text;
  const n = Number(a.channels);
  if (!n) return '';
  return { 1: '1.0', 2: '2.0', 6: '5.1', 8: '7.1' }[n] || n + '声道';
}

function audioTrackSummary(a) {
  if (!a) return '';
  const profile = String(a.profile || '');
  const title = String(a.title || '');
  const blob = (profile + ' ' + title).toLowerCase();
  const codec = String(a.codec || '').toLowerCase();
  let tag;
  const atmos = !!a.atmos || (/atmos|joc/.test(blob) && ['truehd', 'eac3', 'ac3'].includes(codec));
  if (atmos) {
    const type = a.atmos_type || (codec === 'truehd' ? 'TrueHD Atmos' : 'DD+ JOC');
    tag = 'Atmos·' + (type === 'TrueHD Atmos' ? 'TrueHD' : 'DD+');
  } else if (a.dts_x || /dts[:\-\s]?x/.test(blob)) {
    tag = 'DTS:X';
  } else if (a.dts_profile && /DTS-HD/.test(a.dts_profile)) {
    tag = a.dts_profile;
  } else if (codec === 'dts' && /ma/.test(blob)) {
    tag = 'DTS-HD MA';
  } else if (codec === 'dts' && /hd/.test(blob)) {
    tag = 'DTS-HD';
  } else {
    tag = a.codec_label || CODEC_TEXT[codec] || codec.toUpperCase();
  }
  const layout = channelLayout(a);
  return [tag, layout].filter(Boolean).join(' ');
}

function pickAudioTrack(tracks) {
  if (!Array.isArray(tracks) || !tracks.length) return null;
  const immersive = tracks.find((a) => a && (a.atmos || a.dts_x
    || /atmos|dts[:\-\s]?x/i.test(String(a.profile || '') + ' ' + String(a.title || ''))));
  if (immersive) return immersive;
  return tracks.slice().sort((a, b) => {
    const ca = Number(a && a.channels) || 0;
    const cb = Number(b && b.channels) || 0;
    return cb - ca;
  })[0];
}

function audioSummary(tracks) {
  const best = pickAudioTrack(tracks);
  if (!best) return '';
  const tag = audioTrackSummary(best);
  const extra = tracks.length - 1;
  return tag + (extra > 0 ? ' +' + extra : '');
}

function qualityAudioSummary(v) {
  if (Array.isArray(v.audio_details) && v.audio_details.length) return audioSummary(v.audio_details);
  if (Array.isArray(v.audio) && v.audio.length && typeof v.audio[0] === 'object') return audioSummary(v.audio);
  const codec = String(v.audio_codec || '');
  let tag = codec;
  if (v.has_atmos) {
    const kind = codec === 'TrueHD' ? 'TrueHD' : (codec === 'E-AC-3' ? 'DD+' : '');
    tag = 'Atmos' + (kind ? '·' + kind : '');
    if (!kind) tag += ' ' + codec;
  }
  const layout = channelLayout({ channels: v.audio_channels, channel_layout: v.audio_channels });
  return [tag, layout].filter(Boolean).join(' ');
}

function ratingLabel(score, tier) {
  const text = QUALITY_TIER_TEXT[tier] || tier || '';
  const value = score === undefined || score === null || score === '' ? '' : score;
  return [text, value].filter(Boolean).join(' ');
}

function searchRows(arr, opts = {}) {
  return arr.map((r) => ({
    标题: r.title || r.name || '',
    来源: r.source || r.indexer || '',
    大小: shortSize(r.size),
    做种: r.seeders === undefined || r.seeders === null ? '' : r.seeders,
    相关性: relevanceText(r, opts.query || ''),
  }));
}

function qualityRows(arr) {
  return arr.map((v) => {
    const detail = v.video_details || null;
    return {
      名称: v.name || fileNameOf(v.path),
      分辨率: v.resolution || (detail && detail.resolution) || '',
      编码: codecLabel(v.video_codec || (detail && detail.codec), v.bit_depth || (detail && detail.bit_depth)),
      画质: rangeLabel(detail || { hdr: v.hdr, hdr_format: v.hdr }, v.probe_ok),
      音轨: qualityAudioSummary(v),
      大小: shortSize(v.size_bytes),
      评级: ratingLabel(v.quality_score, v.quality_tier),
    };
  });
}

// 行内展开/详情抽屉：把主表里收起来的字段在这里完整展示（含 ffprobe 技术规格）
function hasDolbyVision(video) {
  return !!video && (!!video.dolby_vision || video.dv_profile != null
    || /dolby vision/i.test(String(video.hdr_format || video.hdr || '')));
}

function addVideoSpecDetail(video, add, probed, fallbackBitRate) {
  if (!video) return;
  add('视频编码', codecLabel(video.codec, video.bit_depth));
  add('编码 Profile', video.profile);
  if (video.width && video.height) add('实际画幅', video.width + '×' + video.height);
  add('像素格式', video.pixel_format);
  add('位深', video.bit_depth ? video.bit_depth + ' bit' : '');
  add('帧率', video.fps ? video.fps + ' fps' : '');
  if (video.bit_rate) add('视频码率', fmtBitRate(video.bit_rate));
  else if (fallbackBitRate) add('整体码率', fmtBitRate(fallbackBitRate));
  add('HDR 类型', rangeLabel(video, probed));
  if (hasDolbyVision(video)) {
    add('杜比视界', video.dv_profile_label || '是');
    add('DV 兼容层', video.dv_compatibility_label);
    add('DV 层结构', video.dv_layers);
    add('DV 等级', video.dv_level);
    add('DV 兼容标识（dv_bl_signal_compatibility_id）', video.dv_bl_signal_compatibility_id);
  } else if (probed) {
    add('杜比视界', '否');
  }
  add('色彩空间', video.color_space);
  add('色域', video.color_primaries);
  add('传输特性', video.color_transfer);
}

function addAudioTrackDetail(tracks, add) {
  const list = Array.isArray(tracks) ? tracks : [];
  if (!list.length) return;
  list.forEach((a, i) => {
    if (!a || typeof a !== 'object') return;
    const parts = [
      a.language ? String(a.language) : '',
      audioTrackSummary(a),
      fmtBitRate(a.bit_rate),
      fmtSampleRate(a.sample_rate),
      a.bit_depth ? a.bit_depth + ' bit' : '',
      a.title ? String(a.title) : '',
    ].filter(Boolean);
    add('音轨 ' + (i + 1), parts.join(' · '));
  });
}

function mediaDetail(src, mode) {
  const flat = {};
  const add = (key, value) => {
    if (value !== null && value !== undefined && value !== '') flat[key] = value;
  };

  if (mode === 'videos') {
    const media = src.media || {};
    const video = media.video || null;
    const tracks = Array.isArray(media.audio) ? media.audio : [];
    const sub = src.subtitle || {};
    const ext = Array.isArray(sub.external) ? sub.external : [];
    const emb = Array.isArray(sub.embedded) ? sub.embedded : [];
    add('名称', src.name);
    add('完整路径', src.path);
    add('相对路径', src.relative_path);
    add('大小', src.size_bytes);
    add('修改时间', src.mtime);
    if (src.name && src.name.includes('.')) add('扩展名', src.name.split('.').pop().toLowerCase());
    add('时长', media.duration_seconds);
    add('容器', media.container);
    add('探测', src.probed ? '成功' : (src.probe_error ? '失败：' + src.probe_error : '未探测'));
    if (src.media_type && src.media_type.type) {
      add('媒体类型', [src.media_type.type,
        src.media_type.confidence != null ? '置信度 ' + src.media_type.confidence : ''].filter(Boolean).join(' '));
    }
    addVideoSpecDetail(video, add, src.probed, media.bit_rate);
    addAudioTrackDetail(tracks, add);
    add('外挂字幕', ext.length
      ? ext.map((s) => s.name + (Array.isArray(s.languages) && s.languages.length
        ? '（' + s.languages.join('/') + '）' : '')).join('；')
      : '');
    add('内嵌字幕', emb.length
      ? emb.map((s) => [s.language || '未知语言', s.codec,
        s.forced ? 'forced' : '', s.title || ''].filter(Boolean).join(' ')).join('；')
      : '');
    add('中文字幕', src.has_chinese_subtitle
      ? '有' + (Array.isArray(src.chinese_subtitle_sources) && src.chinese_subtitle_sources.length
        ? '（' + src.chinese_subtitle_sources.join('、') + '）' : '')
      : (src.probed ? '无' : '未探测'));
    return renderDetail(flat);
  }

  const video = src.video_details || {
    codec: src.video_codec,
    bit_depth: src.bit_depth,
    bit_rate: src.bit_rate,
    hdr: src.hdr,
    hdr_format: src.hdr,
  };
  const tracks = src.audio_details || src.audio || [];
  add('名称', src.name);
  add('完整路径', src.path);
  add('大小', src.size_bytes);
  add('时长', src.duration_seconds);
  add('探测', src.probe_ok ? '成功' : (src.probe_error ? '失败：' + src.probe_error : '未探测'));
  add('来源类型', src.source_type);
  add('分辨率', src.resolution);
  addVideoSpecDetail(video, add, src.probe_ok, src.bit_rate);
  if (Array.isArray(tracks) && tracks.length) {
    addAudioTrackDetail(tracks, add);
  } else if (src.audio_codec || src.has_atmos) {
    const legacyCodec = String(src.audio_codec || '').toLowerCase();
    addAudioTrackDetail([{
      codec: legacyCodec,
      codec_label: src.audio_codec,
      channels: src.audio_channels,
      channel_layout: src.audio_channels,
      atmos: src.has_atmos,
      atmos_type: src.has_atmos
        ? (src.audio_codec === 'TrueHD' ? 'TrueHD Atmos'
          : (src.audio_codec === 'E-AC-3' ? 'DD+ JOC' : 'Atmos')) : null,
    }], add);
  }
  add('评分', src.quality_score);
  add('档位', QUALITY_TIER_TEXT[src.quality_tier] || src.quality_tier);
  add('低质量', src.low_quality ? '是' : '');
  return renderDetail(flat);
}

function regionRows(arr) {
  return arr.map((v) => ({
    名称: v.name || fileNameOf(v.path),
    产地: REGION_TEXT[v.region] || v.region || '',
    置信度: v.confidence === undefined || v.confidence === null ? '' : v.confidence,
    音轨语言: Array.isArray(v.audio_languages) ? v.audio_languages.join('、') : '',
    探测: v.probe_ok ? '成功' : '失败',
  }));
}

function magnetRows(arr) {
  return arr.map((m) => ({
    标题: m.title || '',
    大小: shortSize(m.size),
    磁力: m.magnet || (m.resolve_failed ? '解析失败' : ''),
    更新时间: m.updated || '',
  }));
}

function panRows(arr) {
  return arr.map((p) => ({
    标题: p.title || '',
    网盘: p.host || '',
    链接: p.url || (p.resolve_failed ? '解析失败' : ''),
    核验: p.verified ? '已验证' : (p.confidence || ''),
    更新时间: p.updated || '',
    提示: p.warning || (p.mismatch ? '可能不匹配' : ''),
  }));
}

function linkRows(arr) {
  return arr.map((x) => ({
    标题: x.title || x.name || '',
    ID: x.id === undefined || x.id === null ? '' : x.id,
    链接: x.url || '',
  }));
}

function showRows(arr) {
  const eps = (list) => (Array.isArray(list) ? (list.length ? list.join('、') : '无') : '');
  const count = (list) => (Array.isArray(list) ? list.length : '');
  // 多季剧的 expected/actual/missing 是「跨季并集」，不是集数，
  // 必须按季展开显示，否则会给出误导性的数字。
  const perSeason = (s) => (Array.isArray(s.seasons) && s.seasons.length
    ? s.seasons.map((ps) => {
      const miss = Array.isArray(ps.missing)
        ? (ps.missing.length ? ps.missing.map((e) => 'E' + e).join('、') : '无')
        : '未知';
      return 'S' + (ps.season === null || ps.season === undefined ? '?' : ps.season) + ': ' + miss;
    }).join('；')
    : '');
  return arr.map((s) => {
    const multi = Array.isArray(s.seasons) && s.seasons.length > 0;
    return {
      标题: s.title || '',
      年份: s.year === undefined || s.year === null ? '' : s.year,
      季: multi ? s.seasons.map((ps) => ps.season).filter((v) => v !== null).join('/') : (s.season === undefined || s.season === null ? '' : s.season),
      应有集数: multi ? '' : count(s.expected),
      实有集数: multi ? '' : count(s.actual),
      缺失集: multi ? perSeason(s) : eps(s.missing),
      说明: MISSING_REASON_TEXT[s.reason] || s.reason || (Array.isArray(s.partial_seasons) && s.partial_seasons.length
        ? '部分季无法判断：S' + s.partial_seasons.join('、S') : ''),
    };
  });
}

function groupRows(arr) {
  return arr.map((g) => {
    const members = Array.isArray(g.members) ? g.members : [];
    const best = members[g.best_index] || {};
    return {
      标题: g.title || '',
      年份: g.year === undefined || g.year === null ? '' : g.year,
      类型: g.media_type || '',
      成员数: g.member_count === undefined || g.member_count === null ? members.length : g.member_count,
      最佳版本: g.best_path || best.path || best.name || '',
      时长矛盾: g.duration_mismatch ? '是' : '',
    };
  });
}

function multiRootRows(arr) {
  return arr.map((m) => ({
    标题: m.title || '',
    类型: m.media_type || '',
    'TMDB ID': m.tmdb_id === undefined || m.tmdb_id === null ? '' : m.tmdb_id,
    根数: Array.isArray(m.roots) ? m.roots.length : '',
    剧目根: Array.isArray(m.roots) ? m.roots.join('；') : '',
  }));
}

function seasonRows(arr) {
  const len = (v) => (Array.isArray(v) ? v.length : '');
  return arr.map((s) => ({
    剧名: s.title || '',
    季: s.season === undefined || s.season === null ? '' : s.season,
    总集数: s.total === undefined || s.total === null ? '' : s.total,
    缺失: s.missing_count === undefined || s.missing_count === null ? len(s.missing) : s.missing_count,
    策略: FILL_STRATEGY_TEXT[s.strategy] || s.strategy || '',
    计划: len(s.planned),
    已补: len(s.filled),
    失败: len(s.failed),
    跳过: len(s.skipped),
  }));
}

function planRows(arr) {
  return arr.map((i) => ({
    动作: PLAN_ACTION_TEXT[i.action] || i.action || '',
    类型: i.kind || '',
    级别: i.level === undefined || i.level === null ? '' : i.level,
    源: i.source || '',
    目标: i.target || '',
    原因: i.reason || '',
    置信度: i.confidence === undefined || i.confidence === null ? '' : i.confidence,
  }));
}

function itemRows(arr) {
  const sample = arr.find((x) => x && typeof x === 'object') || {};
  if ('classification' in sample || 'cleanup_allowed' in sample) {
    return arr.map((i) => ({
      路径: i.path || '',
      大小: shortSize(i.size),
      分类: CLEANUP_CLASS_TEXT[i.classification] || i.classification || '',
      动作: CLEANUP_ACTION_TEXT[i.action] || i.action || '',
      可清理: i.cleanup_allowed ? '是' : '否',
      保护原因: CLEANUP_PROTECT_TEXT[i.protection_reason] || i.protection_reason || '',
      匹配规则: i.matched_pattern || '',
    }));
  }
  if ('restore_to' in sample) {
    return arr.map((i) => ({
      类型: i.type || '',
      路径: i.path || '',
      恢复到: i.restore_to || '',
      状态: ROLLBACK_STATUS_TEXT[i.status] || (i.error ? '失败' : (i.status || '已回滚')),
      原因: i.reason || i.error || '',
    }));
  }
  return arr.map((i) => ({
    名称: i.name || fileNameOf(i.path),
    路径: i.path || '',
    状态: INTEGRITY_STATUS_TEXT[i.status] || i.status || '',
    探测: i.probe_ok ? '成功' : '失败',
    原因: Array.isArray(i.reasons) ? i.reasons.join('、') : '',
    容器: i.container || '',
    时长: i.duration_seconds === undefined || i.duration_seconds === null ? '' : fmtDuration(i.duration_seconds),
    大小: shortSize(i.size_bytes),
  }));
}

function muxPlanRows(arr) {
  return arr.map((m) => ({
    视频: m.video_path || '',
    目标文件: m.target_path || '',
    策略: m.strategy || '',
    已有字幕: m.existing_subtitles === undefined || m.existing_subtitles === null ? '' : m.existing_subtitles,
    外挂字幕数: Array.isArray(m.external_subtitles) ? m.external_subtitles.length : '',
    新增音轨数: Array.isArray(m.tracks) ? m.tracks.length : '',
  }));
}

function errorsRows(arr) {
  const sample = arr.find((x) => x && typeof x === 'object') || {};
  if ('video_path' in sample) {
    return arr.map((e) => ({
      视频: e.video_path || '',
      目标文件: e.target_path || '',
      错误: e.error || e.code || '',
    }));
  }
  if ('site' in sample) {
    return arr.map((e) => ({
      站点: e.site || e.source || '',
      错误: e.message || e.error || e.code || '',
    }));
  }
  if ('reason' in sample && !('error' in sample)) {
    return arr.map((e) => ({ 路径: e.path || '', 原因: e.reason || '' }));
  }
  return arr.map((e) => ({
    路径: e.path || '',
    错误: e.error || e.message || e.code || '',
  }));
}

function genericRow(row) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) {
    return { [t('内容', '内容')]: row === undefined || row === null ? '' : String(row) };
  }
  const flat = flatten(row, '', 0, {});
  const out = {};
  for (const [k, v] of Object.entries(flat)) out[k] = v;
  if (!Object.keys(out).length) out[t('内容', '内容')] = objectSummary(row);
  return out;
}

function specialRows(key, arr, opts = {}) {
  if (key === 'videos') {
    return arr.map((v) => {
      const media = v.media || {};
      const video = media.video || {};
      const audio = Array.isArray(media.audio) ? media.audio : [];
      return {
        名称: v.name,
        分辨率: video.resolution || '',
        编码: codecLabel(video.codec, video.bit_depth),
        画质: rangeLabel(video, v.probed),
        音轨: audioSummary(audio),
        中字: v.has_chinese_subtitle ? '有' : (v.probed ? '无' : '未探测'),
        大小: v.size_bytes,
      };
    });
  }
  if (key === 'results') {
    const kind = opts.kind || detectResultKind(arr);
    if (kind === 'search') return searchRows(arr, opts);
    if (kind === 'quality') return qualityRows(arr);
    if (kind === 'region') return regionRows(arr);
    if (kind === 'magnet') return magnetRows(arr);
    if (kind === 'pan') return panRows(arr);
    if (kind === 'links') return linkRows(arr);
  }
  if (key === 'candidates') {
    const sample = arr.find((x) => x && typeof x === 'object') || {};
    if ('canonical_title' in sample || 'tmdb_id' in sample) {
      // resolve_media / TMDB 候选：没有站点/评分，别套字幕候选那套列
      return arr.map((c) => ({
        标题: c.canonical_title || c.title || '',
        原名: c.original_title || '',
        年份: c.year === undefined || c.year === null ? '' : c.year,
        类型: c.media_type || '',
        'TMDB ID': c.tmdb_id === undefined || c.tmdb_id === null ? '' : c.tmdb_id,
        语言: c.original_language || '',
        产地: Array.isArray(c.origin_country) ? c.origin_country.join('/') : '',
      }));
    }
    return arr.map((c) => ({
      标题: c.title || '',
      站点: c.site || '',
      评分: c.score === undefined || c.score === null ? '' : c.score,
      格式: c.format || '',
      语言: Array.isArray(c.languages) ? c.languages.join('/') : (c.languages || ''),
      整季包: c.pack_hint ? '是' : '',
      发布名: c.release_name || '',
    }));
  }
  if (key === 'matches') {
    const sample = arr.find((x) => x && typeof x === 'object') || {};
    if ('parsed' in sample) {
      return arr.map((m) => ({
        名称: m.name || '',
        标题: (m.parsed && m.parsed.title) || '',
        年份: (m.parsed && m.parsed.year) === undefined || (m.parsed && m.parsed.year) === null ? '' : m.parsed.year,
        季: (m.parsed && m.parsed.season) === undefined || (m.parsed && m.parsed.season) === null ? '' : m.parsed.season,
        集: (m.parsed && m.parsed.episode) === undefined || (m.parsed && m.parsed.episode) === null ? '' : m.parsed.episode,
        分辨率: (m.parsed && m.parsed.resolution) || '',
        大小: shortSize(m.size_bytes),
        路径: m.path || '',
      }));
    }
    return arr.map((m) => ({
      名称: m.name || '',
      类型: m.type || '',
      类别: m.media_category || '',
      扩展名: m.extension || '',
      大小: shortSize(m.size_bytes),
      修改时间: m.mtime === undefined || m.mtime === null ? '' : m.mtime,
      相对路径: m.relative_path || m.path || '',
    }));
  }
  if (key === 'entries') {
    return arr.map((e) => ({
      名称: e.name, 类型: e.type, 类别: e.media_category || '',
      大小: e.size_bytes, 修改时间: e.mtime, 相对路径: e.relative_path || e.path,
    }));
  }
  if (key === 'filesystems') {
    return arr.map((f) => ({
      [t('挂载点', '磁盘位置')]: f.mount_point,
      [t('文件系统', '磁盘格式')]: f.fstype || '',
      总量: f.total_human,
      可用: f.free_human,
      使用率: f.percent_used,
      [t('采样路径', '检查过的位置')]: f.sampled_from,
    }));
  }
  if (key === 'jobs') {
    return arr.map((j) => ({
      ID: j.id, 工具: j.title || j.tool,
      状态: pick(FRIENDLY.job, j.status, j.status),
      创建时间: j.created_at, 完成时间: j.finished_at, 耗时: j.duration_ms,
    }));
  }
  if (key === 'shows') return showRows(arr);
  if (key === 'groups' || key === 'suspects' || key === 'duplicate_groups') return groupRows(arr);
  if (key === 'multi_root_shows') return multiRootRows(arr);
  if (key === 'seasons') return seasonRows(arr);
  if (key === 'plan') return planRows(arr);
  if (key === 'plans') return muxPlanRows(arr);
  if (key === 'executed') return muxPlanRows(arr);
  if (key === 'site_attempts') {
    return arr.map((a) => ({
      站点: a.site || '',
      状态: SITE_ATTEMPT_STATUS_TEXT[a.status] || a.status || '',
      结果数: a.count === undefined || a.count === null ? '' : a.count,
      最高分: a.best_score === undefined || a.best_score === null ? '' : a.best_score,
      说明: a.reason || a.error || '',
    }));
  }
  if (key === 'errors') return errorsRows(arr);
  if (key === 'failed_items') return itemRows(arr);
  if (key === 'items') return itemRows(arr);
  return arr.map(genericRow);
}


function flatten(obj, prefix, depth, out) {
  for (const [k, v] of Object.entries(obj || {})) {
    if (v === null || v === undefined) continue;
    const key = prefix ? prefix + '.' + k : k;
    if (Array.isArray(v)) continue;
    if (typeof v === 'object') {
      if (depth < 3) flatten(v, key, depth + 1, out);
      continue;
    }
    out[key] = v;
  }
  return out;
}

function columnOrder(rows) {
  const keys = [];
  const seen = new Set();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!seen.has(key)) { seen.add(key); keys.push(key); }
    }
  }
  const rank = (key) => {
    const leaf = key.split('.').pop();
    const idx = PRIORITY.indexOf(leaf);
    return idx === -1 ? 100 : idx;
  };
  keys.sort((a, b) => rank(a) - rank(b));
  return keys.slice(0, 9);
}

// 空结果的主行动按钮：按工具所属场景，把用户送到真正能继续的地方
function emptyActionForTool(tool) {
  const spec = (tool && S.tools[tool]) || {};
  const cat = spec.category || '';
  if (/media_search|jackett|seedhub|dysbk|subhd_search/.test(tool || '')) {
    const label = t('去搜索', '去搜索');
    return { label: label, onClick: () => { location.hash = '#/resource'; } };
  }
  if (cat === '字幕' || /subtitle|mux/.test(tool || '')) {
    const label = t('去找字幕', '去找字幕');
    return { label: label, onClick: () => { location.hash = '#/subtitle'; } };
  }
  if (cat === '文件') {
    const label = t('去媒体浏览', '去翻文件夹');
    return { label: label, onClick: () => { location.hash = '#/browse'; } };
  }
  if (cat === '整理') {
    const label = t('去整理', '去整理');
    return { label: label, onClick: () => { location.hash = '#/organize'; } };
  }
  const label = t('去扫描', '去扫描');
  return { label: label, onClick: () => { location.hash = '#/scan'; } };
}

// 详情抽屉：人类可读的键值列表，链接单独处理（可点开、可复制），原始 JSON 收在最后
function renderDetail(obj) {
  const box = el('div', 'detail-kv');
  if (!obj || typeof obj !== 'object') {
    box.appendChild(el('pre', 'json', String(obj)));
    return box;
  }
  const raw = [];
  for (const [k, v] of Object.entries(obj)) {
    if (v === null || v === undefined || v === '') continue;
    if (typeof v === 'object') {
      if (Array.isArray(v) && !v.length) continue;
      if (!Array.isArray(v)) raw.push([k, v]);
      continue;
    }
    const row = el('div', 'drow');
    if (k === 'warning' || (k === 'mismatch' && v === true)) row.classList.add('warn');
    row.appendChild(el('span', 'dkey', label(k)));
    const text = String(v);
    if (typeof v === 'string' && /^(magnet:|https?:\/\/)/i.test(text)) {
      const wrap = el('span', 'dval');
      const a = el('a', null, text.length > 52 ? text.slice(0, 52) + '…' : text);
      a.href = text;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      wrap.appendChild(a);
      wrap.appendChild(copyButton(text, t('复制', '复制')));
      row.appendChild(wrap);
    } else {
      const span = el('span', 'dval', fmtCell(v, k));
      if (text.length > 40) span.title = text;
      row.appendChild(span);
    }
    box.appendChild(row);
  }
  for (const [k, v] of raw) {
    const row = el('div', 'drow');
    row.appendChild(el('span', 'dkey', label(k)));
    row.appendChild(el('span', 'dval', objectSummary(v)));
    box.appendChild(row);
  }
  return box;
}

function renderTable(key, arr, opts = {}) {
  const section = el('div');
  const title = el('div', 'result-head');
  title.appendChild(el('h3', null, label(key)));
  title.appendChild(el('span', 'badge', arr.length + ' 条'));
  if (opts.note) title.appendChild(el('span', 'muted', opts.note));
  section.appendChild(title);
  let rows;
  try {
    rows = specialRows(key, arr, opts);
  } catch (err) {
    rows = arr.map((item) => ({ 内容: JSON.stringify(item) }));
  }
  // 指定 cols 的表只显示这几列（其余字段留在详情抽屉里），避免 9 列上限把关键字段切掉。
  // 整列为空（缺字段/结构不一致）时直接不显示，避免出现空白列或 undefined。
  const nonEmpty = (c) => rows.some((row) => row[c] !== undefined && row[c] !== null && row[c] !== '');
  const cols = opts.columns && opts.columns.length
    ? opts.columns.filter((c) => nonEmpty(c) && rows.some((row) => c in row))
    : columnOrder(rows).filter(nonEmpty);
  if (!cols.length) {
    section.appendChild(emptyState({
      title: t('没有可显示的行', '这里现在没有内容'),
      hint: t('换个条件再试一次，或看看原始数据。', '换个条件试试，或展开下面的原始数据。'),
      action: opts.tool ? emptyActionForTool(opts.tool) : { label: t('重试', '再试一次'), onClick: () => route() },
    }));
    return section;
  }
  // 行对：显示行与原始对象一一对应，排序后详情仍取得到原始数据。
  const base = rows.map((row, i) => ({ row, src: arr[i] }));
  const isDir = (p) => !!(p.src && isDirEntry(p.src));
  let sort = { col: null, dir: 1 };
  const ordered = () => {
    let pairs = base.slice();
    if (sort.col) {
      const col = sort.col;
      pairs.sort((a, b) => {
        const va = a.row[col];
        const vb = b.row[col];
        let r;
        if (typeof va === 'number' && typeof vb === 'number') r = va - vb;
        else r = String(va ?? '').localeCompare(String(vb ?? ''), 'zh', { numeric: true });
        return r * sort.dir;
      });
    } else if (opts.dirsFirst) {
      pairs.sort((a, b) => (isDir(a) ? 0 : 1) - (isDir(b) ? 0 : 1));
    }
    return pairs;
  };

  const wrap = el('div', 'table-wrap');
  if (opts.fit) wrap.classList.add('fit-wrap');
  // 结果表筛选条：关键词 + 来源，避免在长表里靠肉眼找
  const filter = { q: '', source: '' };
  let filterCount = null;
  let qInput = null;
  let srcSel = null;
  // 统一规则：任何超过 8 行的表都出现同一个筛选条；明确传 filterable:false 才隐藏
  if (opts.filterable !== false && base.length > 8) {
    const bar = el('div', 'table-filter');
    qInput = document.createElement('input');
    qInput.type = 'search';
    qInput.placeholder = opts.filterPlaceholder || t('筛选…（匹配所有列）', '筛选：输入关键词');
    qInput.setAttribute('aria-label', t('筛选结果', '筛选结果'));
    const hasSource = cols.includes('来源');
    if (hasSource) {
      srcSel = document.createElement('select');
      srcSel.setAttribute('aria-label', t('按来源筛选', '按来源筛选'));
      const allOpt = document.createElement('option');
      allOpt.value = '';
      allOpt.textContent = t('全部来源', '全部来源');
      srcSel.appendChild(allOpt);
      const sources = [...new Set(rows.map((r) => r['来源']).filter(Boolean))];
      for (const s of sources) {
        const opt = document.createElement('option');
        opt.value = s;
        opt.textContent = s;
        srcSel.appendChild(opt);
      }
    }
    filterCount = el('span', 'muted', '');
    qInput.addEventListener('input', () => { filter.q = qInput.value.trim().toLowerCase(); visible = pageSize; drawBody(); });
    if (srcSel) srcSel.addEventListener('change', () => { filter.source = srcSel.value; visible = pageSize; drawBody(); });
    bar.append(qInput, ...(srcSel ? [srcSel] : []), filterCount);
    section.appendChild(bar);
  }
  const matches = (row) => {
    if (filter.source && String(row['来源'] || '') !== filter.source) return false;
    if (!filter.q) return true;
    const hay = cols.map((c) => String(row[c] ?? '')).join(' ').toLowerCase();
    return hay.includes(filter.q);
  };
  // 媒体表：固定布局 + 百分比列宽，容器再窄也按比例收缩、文本省略，不产生横向滚动条
  const fitWidths = {
    名称: '24%', 标题: '24%', 分辨率: '8%', 编码: '10%', 画质: '15%', 音轨: '15%',
    大小: '8%', 中字: '7%', 评级: '9%', 状态: '9%', 来源: '10%',
  };
  const table = document.createElement('table');
  if (opts.fit) table.className = 'fit-table';
  const thead = document.createElement('thead');
  const trh = document.createElement('tr');
  const thNodes = [];
  const selectable = !!opts.selectable;
  const selected = new Set((opts.selected || []).map((p) => (typeof p === 'string' ? p : (p.path || p.name || ''))));
  // 选择对象与行 key 分开存：onSelect 要拿到原始行对象（浏览页要按目录过滤），不能只给路径字符串
  const selectedSrc = new Map();
  const keyOf = (p) => (p.src && (p.src.path || p.src.name)) || p.row.path || p.row.name || '';
  const srcByKey = new Map(base.map((p) => [keyOf(p), p.src]));
  for (const key of selected) {
    if (srcByKey.has(key)) selectedSrc.set(key, srcByKey.get(key));
  }
  const notifySelect = () => { if (opts.onSelect) opts.onSelect([...selectedSrc.values()]); };
  let headCb = null;
  if (selectable) {
    headCb = document.createElement('input');
    headCb.type = 'checkbox';
    headCb.setAttribute('aria-label', t('全选当前显示的行', '全选这一页'));
    const th = el('th', 'select-col');
    th.appendChild(headCb);
    trh.appendChild(th);
  }
  for (const col of cols) {
    const th = el('th', 'sortable');
    if (opts.fit && fitWidths[col]) th.style.width = fitWidths[col];
    if (opts.fit && (col === '名称' || col === '标题')) th.classList.add('sticky-col');
    th.appendChild(el('span', null, label(col)));
    const caret = el('i', 'sort-ind', '');
    th.appendChild(caret);
    th.title = t('点击排序', '点一下可以排序');
    th.addEventListener('click', () => {
      if (sort.col === col) sort.dir = -sort.dir;
      else { sort = { col, dir: 1 }; }
      thNodes.forEach((n, i) => {
        const on = cols[i] === sort.col;
        n.classList.toggle('on', on);
        n.querySelector('.sort-ind').textContent = on ? (sort.dir === 1 ? '▲' : '▼') : '';
      });
      drawBody();
    });
    thNodes.push(th);
    trh.appendChild(th);
  }
  const actionsTh = el('th', 'actions-col', t('操作', '操作'));
  if (opts.fit) actionsTh.style.width = '12%';
  trh.appendChild(actionsTh);
  thead.appendChild(trh);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  table.appendChild(tbody);

  // 分页/加载更多：搜索结果每页 50 条，其他大表每页 300 条；不再静默截断
  const paged = !!opts.paged || base.length > 300;
  const pageSize = paged ? (opts.pageSize || 50) : 300;
  let visible = pageSize;
  const emptyHost = el('div', 'table-empty');
  const moreBar = el('div', 'table-more');
  const clearFilters = () => {
    filter.q = '';
    filter.source = '';
    if (qInput) qInput.value = '';
    if (srcSel) srcSel.value = '';
    visible = pageSize;
    drawBody();
  };
  const drawBody = () => {
    tbody.textContent = '';
    const pairs = ordered().filter((p) => matches(p.row));
    if (filterCount) filterCount.textContent = (filter.q || filter.source) ? pairs.length + ' / ' + base.length : '';
    const shown = Math.min(pairs.length, visible);
    for (let i = 0; i < shown; i += 1) {
      const pair = pairs[i];
      const row = pair.row;
      const tr = document.createElement('tr');
      if (selectable) tr.classList.add('has-select');
      if (/^(不匹配|可疑)/.test(String(row['相关性'] || ''))) tr.classList.add('warn-row');
      const linked = !!(opts.rowLink && opts.rowLink(pair.src, row));
      if (linked) tr.classList.add('row-link');
      const itemPath = keyOf(pair);
      tr.dataset.key = itemPath;
      if (selectable) {
        const cbTd = el('td', 'select-col');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.setAttribute('aria-label', t('选择这一行', '选这一行'));
        cb.dataset.key = itemPath;
        if (selected.has(itemPath)) cb.checked = true;
        cb.addEventListener('click', (ev) => ev.stopPropagation());
        cb.addEventListener('change', () => {
          if (cb.checked) {
            selected.add(itemPath);
            if (pair.src) selectedSrc.set(itemPath, pair.src);
          } else {
            selected.delete(itemPath);
            selectedSrc.delete(itemPath);
          }
          notifySelect();
        });
        cbTd.appendChild(cb);
        tr.appendChild(cbTd);
      }
      for (const col of cols) {
        const value = row[col];
        const numeric = isNumericCol(col, value);
        const td = el('td', numeric ? 'num' : 'wrap', '');
        td.appendChild(cellContent(fmtCell(value, col), numeric));
        td.dataset.label = label(col); // 手机端卡片化时作为字段名
        if (opts.fit && (col === '名称' || col === '标题')) {
          td.classList.add('sticky-col');
          td.title = String(value ?? '');
        } else if (typeof value === 'string' && value.length > 60) td.title = value;
        else if (opts.fit && typeof value === 'string' && value.length > 12) td.title = value;
        tr.appendChild(td);
      }
      const actionTd = el('td');
      actionTd.dataset.label = t('操作', '操作');
      const isVideo = isVideoFile(itemPath);
      const isDir = pair.src && isDirEntry(pair.src);

      if (isVideo) {
        const quickBtn = el('button', 'quick-action-pill');
        quickBtn.appendChild(icon('bolt'));
        quickBtn.appendChild(el('span', 'qa-label', t('快捷操作', '操作')));
        quickBtn.type = 'button';
        quickBtn.title = t('打开单片快捷操作面板（搜字幕/混流/整理/体检）', '打开快捷面板');
        quickBtn.onclick = (ev) => {
          ev.stopPropagation();
          openMediaActionModal(pair.src);
        };
        actionTd.appendChild(quickBtn);
      } else if (isDir && pair.src.path) {
        const orgBtn = el('button', 'ghost small');
        btnContent(orgBtn, 'organize', t('整理', '整理'));
        orgBtn.type = 'button';
        orgBtn.title = t('整理此文件夹下的影视', '规范命名并整理');
        orgBtn.onclick = (ev) => {
          ev.stopPropagation();
          sendToTool('organize_media', { path: pair.src.path });
        };
        actionTd.appendChild(orgBtn);
      }

      if (opts.handoff) {
        let hs = [];
        try { hs = opts.handoff(pair.src, row) || []; } catch (_) { hs = []; }
        for (const item of hs) {
          const hb = el('button', 'ghost small handoff-btn', item.label);
          hb.type = 'button';
          hb.title = t('带这行的参数跳到对应功能', '带着这行的信息去下一步');
          hb.addEventListener('click', (ev) => {
            ev.stopPropagation();
            sendToTool(item.tool, item.args || {});
          });
          actionTd.appendChild(hb);
        }
      }
      const detailBtn = el('button', 'ghost small', '详情');
      detailBtn.type = 'button';
      actionTd.appendChild(detailBtn);
      tr.appendChild(actionTd);
      detailBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const existing = tr.nextElementSibling;
        if (existing && existing.classList.contains('detail')) { existing.remove(); return; }
        const detail = document.createElement('tr');
        detail.className = 'detail';
        const td = document.createElement('td');
        td.colSpan = cols.length + 1 + (selectable ? 1 : 0);
        td.appendChild(opts.detail ? opts.detail(pair.src, row) : renderDetail(pair.src));
        td.appendChild(rawDetails(pair.src));
        detail.appendChild(td);
        tr.after(detail);
      });
      if (linked && opts.activate) {
        tr.addEventListener('click', (ev) => {
          if (ev.target.closest('button, a, input, .select-col')) return;
          opts.activate(pair.src, row);
        });
      } else if (isVideo) {
        tr.classList.add('row-link');
        tr.style.cursor = 'pointer';
        tr.title = t('点击打开单片快捷操作面板', '点击查看快捷操作');
        tr.addEventListener('click', (ev) => {
          if (ev.target.closest('button, a, input, .select-col')) return;
          openMediaActionModal(pair.src);
        });
      }
      tbody.appendChild(tr);
    }
    if (headCb) {
      // 全选只作用于当前可见（过滤后）的行；重绘后表头勾选态要跟着已选行回显
      const cbs = [...tbody.querySelectorAll('input[type="checkbox"][data-key]')];
      const checkedCount = cbs.filter((cb) => cb.checked).length;
      headCb.checked = cbs.length > 0 && checkedCount === cbs.length;
      headCb.indeterminate = checkedCount > 0 && checkedCount < cbs.length;
      headCb.onchange = () => {
        for (const cb of cbs) {
          cb.checked = headCb.checked;
          if (headCb.checked) {
            selected.add(cb.dataset.key);
            if (srcByKey.get(cb.dataset.key)) selectedSrc.set(cb.dataset.key, srcByKey.get(cb.dataset.key));
          } else {
            selected.delete(cb.dataset.key);
            selectedSrc.delete(cb.dataset.key);
          }
        }
        headCb.indeterminate = false;
        notifySelect();
      };
    }
    // 过滤后没有行：把空结果也收进统一空状态，并给一个能继续操作的按钮
    emptyHost.textContent = '';
    moreBar.textContent = '';
    if (!pairs.length) {
      const activeFilter = !!(filter.q || filter.source);
      emptyHost.appendChild(emptyState({
        title: activeFilter ? t('没有符合筛选的结果。', '没有符合条件的结果') : t('这里现在没有内容。', '这里现在没有内容'),
        hint: activeFilter
          ? t('清空筛选条件就能看到全部结果。', '清空上面的筛选就能看到全部。')
          : t('换个位置或条件再试一次。', '换个位置或条件再试一次。'),
        action: activeFilter
          ? { label: t('清空筛选', '清空筛选'), onClick: clearFilters }
          : (opts.tool ? emptyActionForTool(opts.tool) : { label: t('重试', '再试一次'), onClick: () => route() }),
      }));
      return;
    }
    const backendCap = pairs.length >= 500 ? t('；后端单次最多返回 500 条', '；后端一次最多给 500 条') : '';
    moreBar.appendChild(el('span', 'muted', t(`已显示 ${shown} / 共 ${pairs.length} 条${backendCap}`, `显示 ${shown} / 共 ${pairs.length} 条${backendCap}`)));
    if (pairs.length > visible) {
      const more = el('button', 'ghost small', t(`加载更多（还有 ${pairs.length - visible} 条）`, `再看 ${Math.min(pageSize, pairs.length - visible)} 条`));
      more.type = 'button';
      more.addEventListener('click', () => { visible += pageSize; drawBody(); });
      moreBar.appendChild(more);
    }
  };
  drawBody();
  wrap.appendChild(table);
  section.appendChild(wrap);
  section.appendChild(emptyHost);
  section.appendChild(moreBar);
  return section;
}

// ---------------------------------------------------------------- 预设流水线执行器
// 中断时的「跳到对应工具」按钮：流程停在哪一步，就把用户送到哪一步继续
function flowJump(tool, label) {
  const btn = el('button', 'ghost small flow-jump', label || t('跳到对应工具', '去这里继续'));
  btn.type = 'button';
  btn.addEventListener('click', () => sendToTool(tool, {}));
  return btn;
}

// 步骤摘要：折叠后仍能看到这一步产出了什么
function resultBrief(result) {
  if (!result || typeof result !== 'object') return '';
  const summary = result.summary;
  if (summary && typeof summary === 'object') {
    const parts = Object.entries(summary)
      .filter(([, v]) => v != null && v !== '' && typeof v !== 'object')
      .slice(0, 3)
      .map(([k, v]) => label(k) + ' ' + v);
    if (parts.length) return parts.join(' · ');
  }
  if (Array.isArray(result.results)) {
    return result.results.length ? t(`找到 ${result.results.length} 条结果`, `找到 ${result.results.length} 条结果`) : t('没有结果', '没有找到结果');
  }
  const list = Object.entries(result).find(([, v]) => Array.isArray(v) && v.length);
  if (list) return label(list[0]) + ' ' + list[1].length + t(' 项', ' 项');
  return '';
}

// 串行跑 FLOWS 的步骤：上一步结果注入下一步；缺参数/门控关闭/失败都会停下来交给用户。
async function runFlow(flow, base, log) {
  log.textContent = '';
  let prev = null;
  for (let i = 0; i < flow.steps.length; i += 1) {
    const step = flow.steps[i];
    const spec = S.tools[step.tool];
    // 一层流程 → 一层步骤（可折叠摘要）→ 步骤内的结果表
    const stepCard = el('details', 'flow-step');
    stepCard.open = true;
    const head = el('summary', 'flow-step-head');
    const badge = el('span', 'badge warn', t('进行中', '进行中'));
    const title = el('strong', null, `${i + 1}. ` + (spec ? pick(FRIENDLY.tool, step.tool, spec.title) : step.tool));
    const brief = el('span', 'flow-step-brief', '');
    head.append(badge, title, brief);
    const body = el('div', 'flow-step-body');
    stepCard.append(head, body);
    log.appendChild(stepCard);
    stepCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    if (!spec) {
      badge.className = 'badge err';
      badge.textContent = t('不可用', '用不了');
      body.textContent = t('这个功能在当前应用里不存在，刷新页面再试。', '没有这个功能，刷新页面再试。');
      return;
    }
    let args = null;
    try { args = step.argsFrom(prev, base); } catch (_) { args = null; }
    if (!args) {
      badge.className = 'badge';
      badge.textContent = t('待补参数', '等你补充');
      brief.textContent = t('缺参数，已停下', '缺信息，停在这里');
      body.textContent = t('缺少参数，已停下。到对应功能页填写后继续。', '少了信息，先停下来；到对应功能里填好再继续。');
      body.appendChild(flowJump(step.tool, t('去补参数', '去补参数')));
      return;
    }
    const gateCfg = S.config || {};
    const offGates = (TOOL_GATES[step.tool] || []).filter((g) => gateCfg[g] === false);
    if (offGates.length && args.dry_run !== true) {
      badge.className = 'badge err';
      badge.textContent = t('被配置拦下', '没权限');
      brief.textContent = t('缺配置，已停下', '没权限，停在这里');
      body.textContent = t(`需要先在「配置」页开启：${offGates.join('、')}`,
        `要先到「配置」页打开「${offGates.map((g) => GATE_LABELS[g]).join('」和「')}」`);
      const conf = el('button', 'ghost small flow-jump', t('去配置打开权限', '去打开权限'));
      conf.type = 'button';
      conf.addEventListener('click', () => { location.hash = '#/config'; });
      body.append(conf, flowJump(step.tool));
      return;
    }
    try {
      let result;
      if (spec.long_running) {
        body.textContent = t('已提交后台任务…', '交给后台了…');
        const created = await api('/api/jobs', { method: 'POST', body: { tool: step.tool, args } });
        pollJobsIndicator();
        const job = await pollJob(created.job.id, body);
        if (job.status !== 'succeeded' || !job.result) {
          throw job.error || { code: job.status, message: '任务未成功' };
        }
        result = job.result;
      } else {
        body.textContent = t('执行中…', '正在处理…');
        result = await api('/api/tools/' + encodeURIComponent(step.tool), { method: 'POST', body: { args } });
      }
      if (result && result.ok === false) throw result.error || { code: 'failed', message: '工具返回失败' };
      badge.className = 'badge ok';
      badge.textContent = t('完成', '搞定');
      body.textContent = '';
      renderResult(result, body, { tool: step.tool, args });
      prev = result;
      brief.textContent = resultBrief(result);
      stepCard.open = false; // 完成的步骤收成一行摘要，结果表仍可展开
    } catch (err) {
      badge.className = 'badge err';
      badge.textContent = t('中断', '出错了');
      brief.textContent = t('出错，已停下', '出错了，停在这里');
      body.textContent = '';
      body.appendChild(errorBox({ code: err.code || 'request_failed', message: err.message || String(err) }));
      body.appendChild(flowJump(step.tool));
      stepCard.open = true;
      return;
    }
  }
}

// ---------------------------------------------------------------- 概览
// 图表只用现有接口字段：/api/overview（磁盘卷）、/api/jobs（任务状态）、/api/meta（工具分类）。
// 纯 CSS 条形，不引入任何外部库；颜色全部走 token。
function chartBox(titleText, noteText) {
  const box = el('div', 'chart');
  const head = el('div', 'chart-head');
  head.appendChild(el('span', 'chart-title', titleText));
  if (noteText) head.appendChild(el('span', 'chart-note', noteText));
  box.appendChild(head);
  return box;
}

function chartEmpty(box, text) {
  box.appendChild(el('div', 'chart-empty', text));
  return box;
}

function chartBarRow(labelText, value, max, valueText, tone) {
  const row = el('div', 'chart-row');
  row.appendChild(el('span', 'chart-label', labelText));
  const track = el('div', 'chart-track');
  const fill = el('i', 'chart-fill' + (tone ? ' ' + tone : ''));
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  fill.style.width = pct.toFixed(2) + '%';
  track.appendChild(fill);
  row.appendChild(track);
  row.appendChild(el('span', 'chart-value', valueText));
  row.title = labelText + '：' + valueText;
  return row;
}

// 存储：每个卷一条「已用 / 可用」堆叠条，百分比与绝对数值都能直接读
function storageUsageChart(filesystems) {
  const box = chartBox(t('存储占用', '存储占用'), t('按卷统计', '按磁盘统计'));
  if (!filesystems.length) return chartEmpty(box, t('暂时读不到磁盘容量。', '暂时读不到磁盘容量。'));
  const rows = el('div', 'chart-rows');
  const legend = el('div', 'chart-legend');
  for (const fs of filesystems) {
    const total = Number(fs.total_bytes) || 0;
    const used = Number(fs.used_bytes) || 0;
    const free = Number(fs.free_bytes) || 0;
    const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
    const row = el('div', 'chart-row');
    row.appendChild(el('span', 'chart-label', fs.mount_point || fs.device || t('磁盘', '磁盘')));
    const stack = el('div', 'chart-stack');
    const u = el('i', 'used');
    u.style.width = pct.toFixed(2) + '%';
    const f = el('i', 'free');
    f.style.width = (100 - pct).toFixed(2) + '%';
    stack.append(u, f);
    row.appendChild(stack);
    row.appendChild(el('span', 'chart-value', total > 0 ? pct.toFixed(1) + '%' : '—'));
    row.title = total > 0
      ? `${fs.used_human || fmtSize(used)} 已用 / ${fs.free_human || fmtSize(free)} 可用`
      : t('这块磁盘没有返回容量数据', '这块磁盘没有返回容量数据');
    rows.appendChild(row);
    const item = el('span');
    item.appendChild(el('i', 'k'));
    const usedText = fs.used_human || fmtSize(used);
    const freeText = fs.free_human || fmtSize(free);
    item.appendChild(document.createTextNode(
      t(`已用 ${usedText} · 可用 ${freeText}`, `已用 ${usedText}，还能放 ${freeText}`)));
    legend.appendChild(item);
  }
  box.appendChild(rows);
  if (legend.childNodes.length) box.appendChild(legend);
  return box;
}

// 任务：按状态计数，值直接标在条子右边
function jobStatusChart(jobList) {
  const box = chartBox(t('任务结果', '任务结果'), t(`最近 ${jobList.length} 条`, `最近 ${jobList.length} 条`));
  if (!jobList.length) {
    return chartEmpty(box, t('还没有后台任务记录。扫描、整理或批量补字幕之后，这里会出现统计。',
      '这里还空空的。扫描或整理之后，这里会出现统计。'));
  }
  const defs = [
    ['succeeded', t('完成', '完成'), 'ok'],
    ['failed', t('失败', '失败'), 'err'],
    ['interrupted', t('已中断', '已中断'), 'err'],
    ['running', t('进行中', '进行中'), 'warn'],
    ['queued', t('排队中', '排队中'), 'warn'],
    ['canceled', t('已取消', '已取消'), 'muted'],
  ];
  const counts = {};
  for (const job of jobList) counts[job.status] = (counts[job.status] || 0) + 1;
  const max = Math.max(...defs.map(([status]) => counts[status] || 0), 1);
  const rows = el('div', 'chart-rows');
  for (const [status, labelText, tone] of defs) {
    const n = counts[status] || 0;
    if (n) rows.appendChild(chartBarRow(labelText, n, max, String(n) + t(' 个', ' 个'), tone));
  }
  // 出现了上面没列出的状态（后端新增）时，兜底成一类，不让统计凭空少掉
  const known = new Set(defs.map(([status]) => status));
  const extra = Object.keys(counts).filter((status) => !known.has(status));
  for (const status of extra) {
    rows.appendChild(chartBarRow(status, counts[status], max, String(counts[status]) + t(' 个', ' 个')));
  }
  box.appendChild(rows);
  return box;
}

// 工具构成：按 /api/meta 里的 category 计数
function toolCategoryChart() {
  const tools = (S.meta && S.meta.tools) || [];
  const box = chartBox(t('工具构成', '工具构成'), t(`${tools.length} 个工具`, `${tools.length} 个工具`));
  if (!tools.length) return chartEmpty(box, t('暂时读不到工具清单。', '暂时读不到工具清单。'));
  const counts = {};
  for (const tool of tools) {
    const name = tool.category || t('其他', '其他');
    counts[name] = (counts[name] || 0) + 1;
  }
  const items = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  const max = Math.max(...items.map((name) => counts[name]), 1);
  const rows = el('div', 'chart-rows');
  for (const name of items) {
    rows.appendChild(chartBarRow(name, counts[name], max, String(counts[name]) + t(' 个', ' 个')));
  }
  box.appendChild(rows);
  return box;
}

async function pageOverview(view) {
  setHeader('概览', t('能力总览、存储空间与写权限状态。', '看看 NAS 里有什么、空间还剩多少。'));
  let jobsError = null;
  const [overview, jobs] = await Promise.all([
    api('/api/overview'),
    api('/api/jobs?limit=60').catch((err) => { jobsError = err; return { jobs: [] }; }),
  ]);
  const app = overview.app || {};
  const cfg = overview.config || {};
  const disk = overview.disk || {};
  const filesystems = disk.filesystems || [];
  const toolsCount = app.tool_count || (S.meta && S.meta.tools.length) || 0;

  // 首屏引导：第一次来的人先看到「按这三步」，数字卡排在后面
  const allowedCount = (cfg.allowed_paths || []).length;
  const guide = el('article', 'card start-card');
  const ghead = el('div', 'card-head');
  ghead.appendChild(el('h2', null, t('开始使用', '第一次用？按这三步')));
  ghead.appendChild(el('span', 'muted', t('不按顺序也行，左侧功能页随时能进', '不按顺序也行，左边随时能点')));
  guide.appendChild(ghead);
  const gbody = el('div', 'card-body start-steps');
  const mkStep = (num, titleText, hintText, acts) => {
    const box = el('div', 'start-step');
    box.appendChild(el('span', 'num', String(num)));
    const main = el('div', 'st-main');
    main.appendChild(el('div', 'st-title', titleText));
    main.appendChild(el('div', 'st-hint', hintText));
    if (acts.length) {
      const row = el('div', 'st-acts');
      for (const [labelText, page, ghost] of acts) {
        const btn = el('button', (ghost ? 'ghost' : 'primary') + ' small', labelText);
        btn.type = 'button';
        btn.addEventListener('click', () => { location.hash = '#/' + page; });
        row.appendChild(btn);
      }
      main.appendChild(row);
    }
    box.appendChild(main);
    return box;
  };
  gbody.appendChild(mkStep(1, t('设定可访问的文件夹', '先设置能访问的文件夹'),
    allowedCount
      ? t(`当前有 ${allowedCount} 个文件夹可访问。${cfg.enable_write ? '写入已按需开启。' : '现在只读；需要整理时再去打开写开关。'}`,
        `现在能看 ${allowedCount} 个文件夹。${cfg.enable_write ? '需要改文件时已经可以写。' : '现在只能看；要整理时再去打开开关。'}`)
      : t('还没有设置可访问的文件夹，先加一个媒体库目录。', '还没设置文件夹，先加一个媒体库位置。'),
    [[t('去配置', '去设置'), 'config']]));
  gbody.appendChild(mkStep(2, t('扫描媒体库', '扫一遍媒体库'),
    t('先知道有哪些影片、缺哪几集、有没有损坏。', '先看看都有什么片、缺不缺集、有没有坏文件。'),
    [[t('去扫描', '开始扫描'), 'scan']]));
  gbody.appendChild(mkStep(3, t('补字幕 / 查缺口', '补字幕、查缺集'),
    t('字幕缺失就批量补；怀疑缺集就做缺集检查。', '没字幕就批量补；觉得少了集就查一查。'),
    [[t('去字幕', '去补字幕'), 'subtitle'], [t('去体检', '去检查'), 'scan', true]]));
  const extras = el('div', 'start-extras');
  extras.appendChild(el('span', 'muted', t('也可以直接：', '也可以直接：')));
  for (const [labelText, page] of [
    [t('找资源', '找资源'), 'resource'],
    [t('整理文件', '自动归类'), 'organize'],
    [t('修改配置', '改设置'), 'config'],
  ]) {
    const btn = el('button', 'ghost small', labelText);
    btn.type = 'button';
    btn.addEventListener('click', () => { location.hash = '#/' + page; });
    extras.appendChild(btn);
  }
  gbody.appendChild(extras);
  guide.appendChild(gbody);
  view.appendChild(guide);

  const stats = el('div', 'grid cols-4');
  stats.appendChild(statCard(t('可用工具', '能做的事'), toolsCount + ' 个', `核心 ${app.core_version || ''} · 应用 ${app.version || ''}`));
  stats.appendChild(statCard(t('白名单路径', '允许访问的文件夹'), String((cfg.allowed_paths || []).length), (cfg.allowed_paths || []).join('、') || '未配置'));
  const gates = [
    cfg.enable_write && t('写入', '改文件'),
    cfg.enable_organize && t('整理', '归类'),
    cfg.enable_delete && t('删除', '删文件'),
  ].filter(Boolean);
  stats.appendChild(statCard(t('写操作门', '允许修改/删除文件'),
    gates.length ? gates.join(' / ') + t(' 已开启', ' 已开启') : t('全部关闭', '暂未开启'),
    t('默认只读，按需在配置页开启', '现在只能看不能改；需要时到「配置」页打开')));
  const total = filesystems.reduce((acc, fs) => acc + (Number(fs.total_bytes) || 0), 0);
  const free = filesystems.reduce((acc, fs) => acc + (Number(fs.free_bytes) || 0), 0);
  stats.appendChild(statCard(t('媒体存储可用', '存储空间还剩'),
    filesystems.length ? fmtSize(free) : '—',
    filesystems.length ? t(`共 ${filesystems.length} 个卷 · 总 ${fmtSize(total)}`, `共 ${filesystems.length} 块磁盘 · 一共 ${fmtSize(total)}`) : t('未获取到卷信息', '暂时读不到磁盘信息')));
  view.appendChild(stats);

  if (filesystems.length) {
    const card = el('article', 'card');
    const head = el('div', 'card-head');
    head.appendChild(el('h2', null, t('存储空间', '磁盘空间')));
    card.appendChild(head);
    const body = el('div', 'card-body');
    body.appendChild(storageUsageChart(filesystems));
    body.appendChild(renderTable('filesystems', filesystems));
    body.appendChild(el('p', 'muted', t('统计范围：媒体库存放位置与允许访问的文件夹；同一块磁盘上的多个文件夹会合并为一行。', '统计了「媒体库存放位置」和「允许访问的文件夹」；同一块硬盘上的几个文件夹会合并成一行。')));
    if (disk.errors && disk.errors.length) {
      body.appendChild(el('div', 'notice', t(`${disk.errors.length} 个路径不可访问：`, `${disk.errors.length} 个文件夹打不开：`) + disk.errors.map((e) => e.path).join('、')));
    }
    card.appendChild(body);
    view.appendChild(card);
  } else if (disk.ok === false && disk.error) {
    view.appendChild(errorBox({ code: 'load_failed', message: t('磁盘信息不可用：', '暂时读不到磁盘信息：') + (disk.error.message || '') }, { retry: () => route() }));
  }

  // 能力与任务：工具分类、后台任务结果；数据都来自已经在用的接口
  const chartsCard = el('article', 'card');
  const chartsHead = el('div', 'card-head');
  chartsHead.appendChild(el('h2', null, t('能力与任务', '能力与任务')));
  chartsHead.appendChild(el('span', 'muted', t('只读现有数据，不额外扫描', '不用额外扫描，直接看现有数据')));
  chartsCard.appendChild(chartsHead);
  const chartsBody = el('div', 'card-body');
  const chartGrid = el('div', 'chart-grid');
  chartGrid.appendChild(toolCategoryChart());
  chartGrid.appendChild(jobStatusChart(jobs.jobs || []));
  chartsBody.appendChild(chartGrid);
  chartsCard.appendChild(chartsBody);
  view.appendChild(chartsCard);

  // 一键流程：把多步手工衔接串成一条流水线（缺参/无权限会停下来交给用户）
  const flowCard = el('article', 'card');
  const fh = el('div', 'card-head');
  fh.appendChild(el('h2', null, t('一键流程', '几件事连着做')));
  fh.appendChild(el('span', 'muted', t('上一步的结果自动带入下一步', '上一步的结果会自动交给下一步')));
  flowCard.appendChild(fh);
  const fbody = el('div', 'card-body');
  const flowList = el('div', 'flows');
  for (const flow of FLOWS) {
    const box = el('div', 'flow');
    const titleRow = el('div', 'result-head');
    titleRow.appendChild(el('h3', null, IS_PRO() ? flow.title : flow.titleFriendly));
    box.appendChild(titleRow);
    const chain = el('div', 'flow-chain');
    flow.steps.forEach((s, i) => {
      if (i) chain.appendChild(el('i', null, '→'));
      const spec = S.tools[s.tool];
      chain.appendChild(el('span', 'chip', spec ? pick(FRIENDLY.tool, s.tool, spec.title) : s.tool));
    });
    box.appendChild(chain);
    const f = el('form', 'flow-form');
    let input;
    if (flow.input === 'path') {
      // 流程也只从已挂载目录里挑，不给手输
      input = buildSelect('flow-path', 'flow-path-' + flow.id, pathOptionsFor(''), { name: 'path' }, null, false);
    } else {
      input = document.createElement('input');
      input.type = 'text';
      input.placeholder = t('搜索关键词', '搜什么词？');
    }
    input.required = true;
    const start = el('button', 'primary', t('开始执行', '开始'));
    start.type = 'submit';
    f.append(input, start);
    box.appendChild(f);
    const log = el('div', 'flow-log');
    box.appendChild(log);
    f.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const value = input.value.trim();
      if (!value) return;
      const base = { [flow.input]: value };
      if (flow.input === 'path') rememberPath(value);
      start.disabled = true;
      try {
        await runFlow(flow, base, log);
      } finally {
        start.disabled = false;
        pollJobsIndicator();
      }
    });
    flowList.appendChild(box);
  }
  fbody.appendChild(flowList);
  flowCard.appendChild(fbody);
  view.appendChild(flowCard);

  const recent = el('article', 'card');
  const rhead = el('div', 'card-head');
  rhead.appendChild(el('h2', null, t('最近任务', '最近做了什么')));
  const more = el('button', 'ghost small', t('全部任务', '看全部'));
  more.type = 'button';
  more.addEventListener('click', () => { location.hash = '#/workbench'; });
  rhead.appendChild(more);
  recent.appendChild(rhead);
  const rbody = el('div', 'card-body');
  const jobList = (jobs.jobs || []).slice(0, 6);
  if (jobsError) {
    rbody.appendChild(errorBox({ code: jobsError.code || 'load_failed', message: jobsError.message }, { retry: () => route() }));
  } else if (!jobList.length) {
    rbody.appendChild(emptyState({
      title: t('还没有后台任务。', '这里还空空的'),
      hint: t('扫描、整理、批量补字幕会自动进入后台队列。', '扫描、整理、批量补字幕时会在后台进行，做完会出现在这里。'),
      action: { label: t('去扫描媒体库', '先扫描一下'), onClick: () => { location.hash = '#/scan'; } },
    }));
  } else {
    rbody.appendChild(renderJobs(jobList, () => route()));
  }
  recent.appendChild(rbody);
  view.appendChild(recent);
}

function statCard(labelText, value, hint) {
  const card = el('div', 'card stat');
  card.appendChild(el('div', 'label', labelText));
  card.appendChild(el('div', 'value', value));
  if (hint) card.appendChild(el('div', 'hint', hint));
  return card;
}

// 任务已用时间：后端只给状态和时间戳，没有阶段/百分比，这里只如实显示时长
function jobStartMs(job) {
  const raw = job.started_at || job.created_at;
  if (!raw) return 0;
  const ms = Date.parse(String(raw).replace(' ', 'T'));
  return Number.isFinite(ms) ? ms : 0;
}

function fmtJobElapsed(state, sec) {
  const s = Math.max(0, Math.round(sec));
  const body = s >= 60
    ? `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, '0')} 秒`
    : `${s} 秒`;
  return state === 'queued'
    ? t(`排队等待 ${body}`, `排队 ${body}`)
    : t(`已运行 ${body}`, `已运行 ${body}`);
}

function tickJobElapsed() {
  document.querySelectorAll('.job-elapsed[data-start]').forEach((node) => {
    const start = Number(node.dataset.start);
    if (!start) return;
    node.textContent = fmtJobElapsed(node.dataset.state, (Date.now() - start) / 1000);
  });
}

function renderJobs(jobs, onRefresh = null) {
  const wrap = el('div');
  for (const job of jobs) {
    const row = el('div', 'job');
    const left = el('div');
    const title = el('div');
    title.appendChild(el('strong', null, job.title || job.tool));
    title.appendChild(document.createTextNode(' '));
    const cls = job.status === 'succeeded' ? 'ok'
      : (job.status === 'failed' || job.status === 'interrupted' ? 'err'
        : (job.status === 'canceled' ? '' : 'warn'));
    title.appendChild(el('span', 'badge ' + cls, pick(FRIENDLY.job, job.status, job.status)));
    if (jobPartial(job)) title.appendChild(el('span', 'badge warn', t('部分结果', '部分结果')));
    if (job.result_stored) title.appendChild(el('span', 'badge', t('结果已存档', '结果已存')));
    if (job.pending) title.appendChild(el('span', 'badge warn', t('还有任务在跑', '还在跑')));
    left.appendChild(title);
    const metaBits = [];
    if (IS_PRO()) metaBits.push(job.id);
    metaBits.push(job.created_at || '');
    if (job.duration_ms != null) metaBits.push((job.duration_ms / 1000).toFixed(1) + 's');
    const meta = el('div', 'meta');
    meta.textContent = metaBits.filter(Boolean).join(' · ');
    const active = job.status === 'running' || job.status === 'queued';
    if (active) {
      const start = jobStartMs(job);
      if (start) {
        const elapsed = el('span', 'job-elapsed muted');
        elapsed.dataset.start = String(start);
        elapsed.dataset.state = job.status;
        elapsed.textContent = job.cancel_requested
          ? t('正在取消，等当前步骤结束', '正在取消，等这一步做完')
          : fmtJobElapsed(job.status, (Date.now() - start) / 1000);
        if (meta.textContent) meta.appendChild(document.createTextNode(' · '));
        meta.appendChild(elapsed);
      }
    }
    left.appendChild(meta);

    // 进度：有 total 画进度条，没有 total 用上面的秒表；阶段与当前项都来自任务记录
    const p = job.progress;
    if (p && (p.label || p.stage || p.done != null)) {
      const prog = el('div', 'job-progress');
      const line = el('div', 'job-progress-line');
      line.appendChild(el('span', 'job-stage', p.label || p.stage || t('处理中', '处理中')));
      if (p.total) line.appendChild(el('span', 'job-count muted', `${p.done || 0} / ${p.total}`));
      prog.appendChild(line);
      if (p.total) {
        const bar = el('div', 'progress');
        const fill = el('i');
        fill.style.width = Math.min(100, Math.round(((p.done || 0) / p.total) * 100)) + '%';
        bar.appendChild(fill);
        prog.appendChild(bar);
      }
      if (p.current) {
        const cur = el('div', 'job-current muted');
        cur.textContent = t(`当前：${p.current}`, `正在弄：${p.current}`);
        cur.title = p.current;
        prog.appendChild(cur);
      }
      left.appendChild(prog);
    }

    row.appendChild(left);
    if (JOB_TERMINAL.has(job.status)) {
      const btn = el('button', 'ghost small', '查看结果');
      btn.type = 'button';
      btn.addEventListener('click', () => showJob(job));
      row.appendChild(btn);
    } else {
      const cancelBtn = el('button', 'ghost small job-cancel', '取消');
      cancelBtn.type = 'button';
      cancelBtn.addEventListener('click', async () => {
        const ok = await confirmDialog({
          title: t('取消这个任务？', '要取消这个任务吗？'),
          intro: t('任务会在下一个处理步骤停下，已完成的部分会保留。',
            '任务会在下一步停下，已经做完的部分会保留。'),
          confirmText: t('取消任务', '取消任务'),
          danger: true,
        });
        if (!ok) return;
        cancelBtn.disabled = true;
        cancelBtn.textContent = t('取消中…', '正在取消…');
        try {
          const data = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' });
          Object.assign(job, data.job || {});
          toast(t('已请求取消，等当前步骤结束', '说了取消，等这一步做完'));
          if (onRefresh) onRefresh();
        } catch (err) {
          cancelBtn.disabled = false;
          cancelBtn.textContent = '取消';
          toast(t('取消失败：' + err.message, '没取消成功：' + err.message), true);
        }
      });
      const actions = el('div', 'job-actions');
      actions.appendChild(cancelBtn);
      actions.appendChild(el('span', 'pulse'));
      row.appendChild(actions);
    }
    wrap.appendChild(row);
  }
  return wrap;
}

async function showJob(job) {
  // 列表接口不内联 result：终态任务（尤其 result_stored）先拉详情再渲染
  if (job && !job.result && (job.result_stored || JOB_TERMINAL.has(job.status))) {
    try {
      const data = await api('/api/jobs/' + encodeURIComponent(job.id));
      if (data.job) job = Object.assign({}, job, data.job);
    } catch (_) {
      // 拉详情失败时保留列表里的元信息，下面按状态给出可读提示
    }
  }
  const view = document.getElementById('view');
  const card = el('article', 'card');
  const head = el('div', 'card-head');
  head.appendChild(el('h2', null, t('任务 ' + job.id, '任务详情')));
  const close = el('button', 'ghost small', '关闭');
  close.type = 'button';
  close.addEventListener('click', () => card.remove());
  head.appendChild(close);
  card.appendChild(head);
  const body = el('div', 'card-body');
  const out = el('div');
  // 部分结果说明与 result.canceled / result.partial 同源，不靠状态推断；
  // interrupted / canceled / partial 三种状态都给一句人话解释。
  if (jobPartial(job)) {
    const note = el('div', 'job-partial-note');
    const text = job.status === 'interrupted'
      ? t('应用重启导致任务中断，下面是已完成的部分。', '应用重启把任务打断了，下面是已经做完的部分。')
      : t('任务已取消，下面是已完成的部分。', '任务取消了，下面是已经做完的部分。');
    note.appendChild(el('strong', null, text));
    body.appendChild(note);
  }
  if (job.result) renderResult(job.result, out, { tool: job.tool, args: job.args || {} });
  else if (job.status === 'canceled') {
    out.appendChild(el('p', 'muted', t('任务已取消，还没有开始执行。', '任务已取消，还没开始做。')));
  } else if (job.status === 'interrupted') {
    out.appendChild(el('p', 'muted', t(
      '任务在应用重启时中断，没有产生结果；需要的话重新运行一次。',
      '任务被应用重启打断了，没有结果；需要的话再跑一次。')));
  } else out.appendChild(errorBox(job.error || { code: job.status, message: '无结果' }));
  body.appendChild(out);
  card.appendChild(body);
  // 有专门的展示位就放进去，避免把结果卡插到整页最顶部、把任务列表顶走
  const host = document.getElementById('job-detail-host');
  if (host) {
    host.textContent = '';
    host.appendChild(card);
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } else {
    view.prepend(card);
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

// ---------------------------------------------------------------- 媒体浏览
function copyText(text) {
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).catch(() => (fallback() ? undefined : Promise.reject(new Error('copy'))));
  }
  return fallback() ? Promise.resolve() : Promise.reject(new Error('copy'));
}

function copyButton(text, labelText) {
  const btn = el('button', 'ghost small', labelText || t('复制路径', '复制路径'));
  btn.type = 'button';
  btn.addEventListener('click', async () => {
    try {
      await copyText(typeof text === 'function' ? text() : text);
      toast(t('已复制到剪贴板', '复制好了'));
    } catch (_) {
      toast(t('复制失败，请手动选择文本复制', '复制失败，手动选中复制吧'), true);
    }
  });
  return btn;
}

// ---------------------------------------------------------------- 浏览范围收口
// 对齐 Jellyfin / Sonarr 的做法：只能从「已挂载目录」往下走，不能翻到挂载根之上。
function allowedRoots() {
  const cfg = S.config || {};
  const list = cfg.allowed_paths_expanded && cfg.allowed_paths_expanded.length
    ? cfg.allowed_paths_expanded
    : (cfg.allowed_paths || []);
  return (list.length ? list : ['/media']).map(normPath);
}

function normPath(p) {
  return '/' + String(p || '').split(/[\\/]+/).filter(Boolean).join('/');
}

// 该路径所属的挂载根；不在任何挂载目录内返回 null
function rootOf(path) {
  const p = normPath(path);
  let best = null;
  for (const root of allowedRoots()) {
    if (p === root || p.startsWith(root === '/' ? '/' : root + '/')) {
      if (!best || root.length > best.length) best = root;
    }
  }
  return best;
}

// 越界路径一律落回第一个挂载目录；挂载目录内部的路径原样保留
function clampToRoots(path) {
  const p = normPath(path);
  return rootOf(p) ? p : allowedRoots()[0];
}

function goBrowse(path) {
  location.hash = '#/browse/' + encodeURIComponent(clampToRoots(path));
}

// 挂载了几个文件夹就列几个：不用猜「另一个挂载目录怎么进」
function mountSwitcher(path) {
  const roots = allowedRoots();
  if (roots.length < 2) return null;
  const cur = clampToRoots(path);
  const active = rootOf(cur) || cur;
  const row = el('div', 'mounts');
  row.setAttribute('role', 'group');
  row.setAttribute('aria-label', t('已挂载的文件夹', '挂载的文件夹'));
  row.appendChild(el('span', 'mounts-label', t('已挂载', '挂载的文件夹')));
  for (const root of roots) {
    const name = root === '/' ? '/' : root.split('/').filter(Boolean).pop();
    const on = root === active;
    const btn = el('button', 'mount' + (on ? ' on' : ''), name);
    btn.type = 'button';
    btn.title = root;
    if (on) btn.setAttribute('aria-current', 'true');
    btn.addEventListener('click', () => goBrowse(root));
    row.appendChild(btn);
  }
  return row;
}

function breadcrumb(path) {
  const nav = el('nav', 'crumbs');
  nav.setAttribute('aria-label', '当前路径');
  const cur = clampToRoots(path);
  const root = rootOf(cur) || cur;
  const rootName = root === '/' ? t('根目录', '根目录') : root.split('/').filter(Boolean).pop();
  const rest = cur.slice(root === '/' ? 0 : root.length);
  const parts = rest.split('/').filter(Boolean);
  let acc = root === '/' ? '' : root;
  const segs = parts.map((part, i) => {
    acc += '/' + part;
    return { part, target: acc, current: i === parts.length - 1 };
  });
  // 深层目录只保留「根 › … › 上一级 › 当前」，点「…」再展开中间层，避免面包屑折成好几行
  const collapsed = segs.length > 3;
  let expanded = false;
  const paint = () => {
    nav.textContent = '';
    const rootBtn = el('button', 'crumb', rootName);
    rootBtn.type = 'button';
    rootBtn.title = root;
    rootBtn.addEventListener('click', () => goBrowse(root));
    nav.appendChild(rootBtn);
    const hiddenCount = collapsed && !expanded ? segs.length - 2 : 0;
    const visible = hiddenCount ? segs.slice(hiddenCount) : segs;
    if (hiddenCount) {
      nav.appendChild(el('i', 'sep', '›'));
      const dots = el('button', 'crumb crumb-more', '…');
      dots.type = 'button';
      dots.title = t(`展开中间 ${hiddenCount} 层`, `展开中间 ${hiddenCount} 层`);
      dots.setAttribute('aria-label', dots.title);
      dots.addEventListener('click', () => { expanded = true; paint(); });
      nav.appendChild(dots);
    }
    for (const seg of visible) {
      nav.appendChild(el('i', 'sep', '›'));
      if (seg.current) {
        const now = el('span', 'crumb current', seg.part);
        now.title = seg.target;
        nav.appendChild(now);
        continue;
      }
      const btn = el('button', 'crumb', seg.part);
      btn.type = 'button';
      btn.title = seg.target;
      btn.addEventListener('click', () => goBrowse(seg.target));
      nav.appendChild(btn);
    }
  };
  paint();
  return nav;
}

async function pageBrowse(view, path) {
  setHeader('媒体浏览', t('浏览白名单目录，或按关键词搜索文件。', '翻一翻 NAS 里的文件夹。'));
  if (path && !rootOf(path)) {
    toast(t('该位置不在允许访问的文件夹内，已回到挂载目录。', '这个文件夹不在允许范围里，已经带你回到挂载目录。'), true);
    history.replaceState(null, '', '#/browse/' + encodeURIComponent(clampToRoots(path)));
  }
  const target = path ? clampToRoots(path) : firstAllowedPath();
  const result = await api('/api/tools/nas_list_directory', { method: 'POST', body: { args: { path: target } } });

  const tabs = [{
    key: 'dir',
    title: t('目录浏览', '翻文件夹'),
    risk: 'read',
    hint: t('按文件夹逐层查看 NAS 文件', '一层层打开 NAS 上的文件夹'),
    build: () => browserCard(target, result),
  }];
  for (const name of ['nas_find_files', 'nas_disk_usage']) {
    if (!S.tools[name]) continue;
    const spec = S.tools[name];
    tabs.push({
      key: name,
      title: spec.title,
      risk: spec.risk,
      note: spec.long_running ? t('后台', '慢') : '',
      hint: `${spec.title} · ${spec.risk_label}`,
      build: () => toolCard(name),
    });
  }
  view.appendChild(tabbedView('browse', tabs, path ? 'dir' : S.activeTab.browse));
}

function browserCard(path, result) {
  const card = el('article', 'card');
  const head = el('div', 'card-head');
  const cur = clampToRoots(path);
  const root = rootOf(cur) || cur;
  const atTop = cur === root;
  const left = el('div', 'head-main');
  const mounts = mountSwitcher(cur);
  if (mounts) left.appendChild(mounts);
  left.appendChild(breadcrumb(cur));
  if (result && result.ok === false) left.appendChild(el('div', 'hint', t('该位置暂时打不开', '这个位置暂时打不开')));
  head.appendChild(left);
  const acts = el('div', 'crumb-actions');
  const up = el('button', 'ghost small', t('返回上级', '上一级'));
  up.type = 'button';
  up.disabled = atTop;   // 挂载根就是浏览起点，不能再往上
  if (atTop) up.title = t('已经是允许访问的顶层文件夹', '这已经是能打开的最上层文件夹');
  up.addEventListener('click', () => {
    if (atTop) return;
    const parent = cur.replace(/\/[^/]+$/, '') || '/';
    goBrowse(parent);
  });
  acts.appendChild(up);

  const refresh = el('button', 'ghost small', t('刷新', '刷新'));
  refresh.type = 'button';
  refresh.addEventListener('click', () => route());
  acts.appendChild(refresh);

  // 低频动作收进「更多」，窄屏下头部不再被五个按钮挤爆
  const more = el('details', 'more-menu');
  const moreSummary = el('summary', 'ghost small more-summary');
  moreSummary.appendChild(document.createTextNode(t('更多', '更多')));
  moreSummary.appendChild(icon('chevron', 'more-caret'));
  moreSummary.title = t('复制路径、整理此目录、体检此目录', '复制路径、整理这里、扫描这里');
  more.appendChild(moreSummary);
  const menu = el('div', 'more-list');
  const copy = copyButton(cur, t('复制当前路径', '复制当前路径'));
  const orgCurBtn = el('button', 'ghost small');
  btnContent(orgCurBtn, 'organize', t('整理此目录', '整理这里'));
  orgCurBtn.type = 'button';
  orgCurBtn.title = t('将此目录带入规范整理工具', '把当前文件夹带入整理工具');
  orgCurBtn.addEventListener('click', () => sendToTool('organize_media', { path: cur }));
  const scanCurBtn = el('button', 'ghost small');
  btnContent(scanCurBtn, 'scan', t('体检此目录', '扫描这里'));
  scanCurBtn.type = 'button';
  scanCurBtn.title = t('对当前目录执行媒体体检扫描', '扫描当前文件夹');
  scanCurBtn.addEventListener('click', () => sendToTool('nas_scan_media', { path: cur }));
  for (const item of [copy, orgCurBtn, scanCurBtn]) {
    item.classList.add('more-item');
    item.addEventListener('click', () => { more.open = false; });
    menu.appendChild(item);
  }
  more.appendChild(menu);
  acts.appendChild(more);
  head.appendChild(acts);
  card.appendChild(head);

  const body = el('div', 'card-body');
  if (!result || result.ok === false) {
    body.appendChild(errorBox((result && result.error) || { code: 'load_failed', message: '目录读取失败' }, { retry: () => route() }));
  } else {
    const entries = Array.isArray(result.entries) ? result.entries : [];
    if (!entries.length) {
      const parent = cur.replace(/\/[^/]+$/, '') || '/';
      body.appendChild(emptyState({
        title: t('目录为空。', '这个文件夹是空的'),
        hint: atTop
          ? t('点上面的其他挂载文件夹，换个位置看看。', '点上面的其他文件夹，换个位置看看。')
          : t('可以点上面的「上一级」回到上层文件夹。', '点上面的「上一级」回上一层看看。'),
        action: atTop
          ? { label: t('刷新看看', '刷新看看'), onClick: () => route() }
          : { label: t('回上一层', '回上一层'), onClick: () => goBrowse(parent) },
      }));
    } else {
      const dirs = entries.filter(isDirEntry).length;
      const toolbar = el('div', 'toolbar');
      toolbar.appendChild(el('span', 'muted',
        t(`${dirs} 个文件夹 · ${entries.length - dirs} 个文件`, `${dirs} 个文件夹 · ${entries.length - dirs} 个文件`)));
      body.appendChild(toolbar);
      const holder = el('div');
      body.appendChild(holder);
      const batchBar = el('div', 'batch-bar');
      batchBar.hidden = true;
      body.appendChild(batchBar);
      let picked = [];
      const paintBatch = () => {
        batchBar.textContent = '';
        if (!picked.length) { batchBar.hidden = true; return; }
        batchBar.hidden = false;
        batchBar.appendChild(el('span', 'batch-count', t(`已选 ${picked.length} 个文件夹`, `选了 ${picked.length} 个文件夹`)));
        const mk = (labelText, tool) => {
          const b = el('button', 'primary small', labelText);
          b.type = 'button';
          b.onclick = () => sendToTool(tool, { path: picked.slice() });
          return b;
        };
        batchBar.appendChild(mk(t('体检所选', '扫描这些'), 'nas_scan_media'));
        batchBar.appendChild(mk(t('补字幕所选', '给这些补字幕'), 'media_fill_subtitles'));
        batchBar.appendChild(mk(t('整理所选', '整理这些'), 'organize_media'));
        const clear = el('button', 'ghost small', t('清空选择', '取消选择'));
        clear.type = 'button';
        clear.onclick = () => { picked = []; holder.querySelectorAll('input[type="checkbox"][data-key]').forEach((cb) => { cb.checked = false; }); paintBatch(); };
        batchBar.appendChild(clear);
      };
      const updateBatch = (rows) => {
        picked = rows.filter(isDirEntry).map((e) => e.path);
        paintBatch();
      };
      // 筛选条统一放在表内（和搜索结果、其他结果表一致）
      holder.appendChild(renderTable('entries', entries, {
        dirsFirst: true,
        rowLink: isDirEntry,
        selectable: true,
        selected: picked,
        onSelect: updateBatch,
        activate: (e) => { if (isDirEntry(e)) goBrowse(e.path); },
        filterable: true,
        filterPlaceholder: t('在当前文件夹里过滤名称…', '在这里找某个文件…'),
      }));
      paintBatch();
    }
  }
  card.appendChild(body);
  return card;
}

// ---------------------------------------------------------------- 配置
function configField(labelText, hint) {
  const field = el('div', 'field');
  const input = document.createElement('input');
  input.type = 'text';
  input.id = nextId('cfg');
  const label = el('label', null, labelText);
  label.htmlFor = input.id;
  field.appendChild(label);
  field.appendChild(input);
  if (hint) field.appendChild(el('div', 'hint', hint));
  return { field, input };
}

function configArea(labelText, hint, opts = {}) {
  const field = el('div', 'field wide');
  const input = document.createElement('textarea');
  input.rows = 3;
  input.spellcheck = false;
  input.id = nextId('cfg');
  const label = el('label', null, labelText);
  label.htmlFor = input.id;
  field.appendChild(label);
  field.appendChild(input);
  if (opts.picker) {
    const bar = el('div', 'config-pick');
    const btn = el('button', 'ghost small');
    btnContent(btn, 'folder', t('从 NAS 选择文件夹…', '从 NAS 里选文件夹…'));
    btn.type = 'button';
    btn.onclick = () => {
      openFilePicker({
        initialPath: lines(input.value)[0] || firstAllowedPath(),
        mode: 'dir',
        onSelect: (selected) => {
          const cur = lines(input.value);
          if (!cur.includes(selected)) cur.push(selected);
          input.value = cur.join('\n');
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
        },
      });
    };
    bar.appendChild(btn);
    bar.appendChild(el('span', 'hint', t('或直接粘贴绝对路径，每行一个', '也可以直接粘贴路径，每行一个')));
    field.appendChild(bar);
  }
  if (hint) field.appendChild(el('div', 'hint', hint));
  return { field, input };
}

function secretField(labelText, configured) {
  const field = el('div', 'field');
  const input = document.createElement('input');
  input.type = 'password';
  input.autocomplete = 'new-password';
  input.id = nextId('cfg');
  const label = el('label', null, labelText);
  label.htmlFor = input.id;
  field.appendChild(label);
  input.placeholder = configured ? '已配置（留空保持不变）' : '未配置';
  field.appendChild(input);
  return { field, input };
}

function switchField(labelText, hint) {
  const field = el('div', 'field');
  const wrap = el('label', 'check');
  const input = document.createElement('input');
  input.type = 'checkbox';
  wrap.appendChild(input);
  wrap.appendChild(el('span', null, labelText));
  field.appendChild(wrap);
  if (hint) field.appendChild(el('div', 'hint', hint));
  return { field, input };
}

// 保存前的变更摘要：只列出「真正会变的东西」，写门打开单独标红
function cfgDiffItems(cfg, updates) {
  const items = [];
  const norm = (v) => String(v || '').replace(/\/+$/, '');
  const addSet = (labelText, before, after) => {
    const b = new Set((before || []).map(norm));
    const a = new Set((after || []).map(norm));
    const added = [...a].filter((x) => !b.has(x));
    const removed = [...b].filter((x) => !a.has(x));
    if (!added.length && !removed.length) return;
    const bits = [];
    if (added.length) bits.push('新增 ' + added.join('、'));
    if (removed.length) bits.push('移除 ' + removed.join('、'));
    items.push({ text: labelText + '：' + bits.join('；') });
  };
  addSet('访问白名单', cfg.allowed_paths, updates.allowed_paths);
  addSet('媒体根目录', cfg.media_roots, updates.media_roots);
  addSet('排除目录', cfg.exclude_paths, updates.exclude_paths);
  const gate = (before, after, text, danger) => {
    if (!!before === !!after) return;
    items.push({ text: text + '：' + (after ? '打开' : '关闭'), danger: !!danger && !!after });
  };
  gate(cfg.enable_write, updates.enable_write, '允许改动文件', false);
  gate(cfg.enable_organize, updates.enable_organize, '允许整理移动', false);
  gate(cfg.enable_delete, updates.enable_delete, '允许删除文件', true);
  const src = (labelText, before, after) => {
    if (!!before !== !!after) items.push({ text: labelText + '：' + (after ? '打开' : '关闭') });
  };
  src('TMDB', cfg.tmdb.enabled, updates.tmdb.enabled);
  src('Jackett', cfg.jackett.enabled, updates.jackett.enabled);
  src('SeedHub', cfg.seedhub.enabled, updates.seedhub.enabled);
  src('网盘分享站', cfg.dysbk.enabled, updates.dysbk.enabled);
  src('SubHD', cfg.subhd.enabled, updates.subhd.enabled);
  src('OpenSubtitles', cfg.opensubtitles.enabled, updates.opensubtitles.enabled);
  src('Shooter', cfg.shooter.enabled, updates.shooter.enabled);
  if (String(cfg.jackett.url || '') !== String(updates.jackett.url || '')) {
    items.push({ text: 'Jackett 地址：' + (cfg.jackett.url || '（空）') + ' → ' + (updates.jackett.url || '（空）') });
  }
  const sec = (labelText, v) => { if (v) items.push({ text: labelText + '：填写了新值' }); };
  sec('TMDB 密钥', updates.tmdb.api_key);
  sec('Jackett 密钥', updates.jackett.api_key);
  sec('OpenSubtitles 密钥', updates.opensubtitles.api_key);
  sec('OpenSubtitles 用户名', updates.opensubtitles.username);
  sec('OpenSubtitles 密码', updates.opensubtitles.password);
  sec('Shooter Token', updates.shooter.token);
  const g0 = cfg.cleanup.active_download_grace_minutes ?? 0;
  if (g0 !== updates.cleanup.active_download_grace_minutes) {
    items.push({ text: '下载中宽限：' + g0 + ' → ' + updates.cleanup.active_download_grace_minutes + ' 分钟' });
  }
  return items;
}

// 通用确认弹窗：可列变更清单，危险操作可要求输入确认词
function confirmDialog(opts = {}) {
  const { title, intro, items = [], confirmText, cancelText = t('取消', '不了'), danger = false, requireWord = '', hideClose = false } = opts;
  return new Promise((resolve) => {
    const overlay = el('div', 'modal-overlay');
    const box = el('div', 'modal-box narrow');
    const head = el('div', 'modal-head');
    head.appendChild(el('h2', null, title || t('确认', '确认一下')));
    const closeBtn = el('button', 'modal-close');
    closeBtn.appendChild(icon('close'));
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', t('关闭', '关闭'));
    closeBtn.onclick = () => finish(false);
    if (!hideClose) head.appendChild(closeBtn);
    box.appendChild(head);

    const body = el('div', 'modal-body');
    if (intro) body.appendChild(el('p', 'muted', intro));
    if (items.length) {
      const ul = el('ul', 'diff-list');
      for (const it of items) {
        const text = typeof it === 'string' ? it : it.text;
        ul.appendChild(el('li', it && it.danger ? 'danger' : null, text));
      }
      body.appendChild(ul);
    }
    let input = null;
    if (requireWord) {
      const wrap = el('div', 'field');
      const label = el('label', null, t('输入「' + requireWord + '」确认', '要真的这么做，请照着输入「' + requireWord + '」'));
      input = document.createElement('input');
      input.type = 'text';
      input.id = nextId('confirm');
      input.placeholder = requireWord;
      input.autocomplete = 'off';
      label.htmlFor = input.id;
      wrap.appendChild(label);
      wrap.appendChild(input);
      body.appendChild(wrap);
    }
    box.appendChild(body);

    const foot = el('div', 'modal-foot');
    const cancel = el('button', 'ghost', cancelText);
    cancel.type = 'button';
    cancel.onclick = () => finish(false);
    const ok = el('button', danger ? 'danger' : 'primary', confirmText || t('确认', '就这样'));
    ok.type = 'button';
    ok.disabled = !!requireWord;
    ok.onclick = () => finish(true);
    if (input) input.addEventListener('input', () => { ok.disabled = input.value.trim() !== requireWord; });
    foot.appendChild(cancel);
    foot.appendChild(ok);
    box.appendChild(foot);

    overlay.appendChild(box);
    overlay.addEventListener('click', (ev) => { if (ev.target === overlay) finish(false); });
    const releaseTrap = modalA11y(overlay, box, title || t('确认', '确认一下'));
    const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); finish(false); } };
    document.addEventListener('keydown', onKey);
    function finish(v) {
      document.removeEventListener('keydown', onKey);
      releaseTrap();
      overlay.remove();
      resolve(v);
    }
    document.body.appendChild(overlay);
    (input || ok).focus();
  });
}

// 配置分组：低频分组默认折叠，summary 里带一句当前状态，避免整页十多个字段铺开
function configGroup(titleText, hintText, open) {
  const root = el('details', 'card cfg-group');
  root.open = open;
  const sum = el('summary', 'card-head');
  sum.appendChild(el('h2', null, titleText));
  const hint = el('span', 'muted cfg-hint', hintText || '');
  sum.appendChild(hint);
  root.appendChild(sum);
  const body = el('div', 'card-body');
  root.appendChild(body);
  return { root, body, hint };
}

async function pageConfig(view) {
  setHeader('配置', t('修改白名单、写开关与数据源密钥；保存后立即生效。', '设置哪些文件夹可以访问、要不要允许改动文件，以及各网站的密钥。保存后马上生效。'));
  const cfg = await api('/api/config');
  S.configDirty = false;
  S.config = cfg;
  const form = el('form', 'tool-form config-actions');
  const refs = {};

  const security = el('article', 'card');
  const sh = el('div', 'card-head');
  sh.appendChild(el('h2', null, t('安全边界', '可以访问哪里')));
  security.appendChild(sh);
  const sbody = el('div', 'card-body');
  const sform = el('div', 'tool-form');
  refs.allowed = configArea(t('白名单 allowed_paths *', '允许访问的文件夹 *'),
    t('所有工具只能访问这些路径；每行一个。', '所有功能都只能读写这里列出的文件夹，每行一个。'), { picker: true });
  refs.allowed.input.value = (cfg.allowed_paths || []).join('\n');
  refs.media = configArea(t('媒体根目录 media_roots（可选）', '媒体库存放位置（可不填）'),
    t('留空则沿用白名单；用于磁盘统计与巡检。', '不填就按上面的文件夹算；用来统计磁盘空间。'), { picker: true });
  refs.media.input.value = (cfg.media_roots || []).join('\n');
  refs.exclude = configArea(t('排除目录 exclude_paths', '不想被碰的文件夹'),
    t('家庭录像、成人内容或不希望被扫描的目录；支持 glob。', '这些文件夹不会被扫描或整理，每行一个。'), { picker: true });
  refs.exclude.input.value = (cfg.exclude_paths || []).join('\n');
  for (const f of [refs.allowed, refs.media, refs.exclude]) sform.appendChild(f.field);
  sbody.appendChild(sform);
  const expandedP = el('p', 'muted');
  const setExpandedText = (list) => {
    expandedP.textContent = t('当前展开：', '实际生效的文件夹：') + (list || []).join('、');
  };
  setExpandedText(cfg.allowed_paths_expanded);
  sbody.appendChild(expandedP);
  security.appendChild(sbody);
  view.appendChild(security);

  const gate = el('article', 'card');
  const gh = el('div', 'card-head');
  gh.appendChild(el('h2', null, t('写操作门', '允许改动文件吗')));
  gate.appendChild(gh);
  const gbody = el('div', 'card-body');
  gbody.appendChild(el('div', 'notice', t('默认只读。开启后对应工具才能真正写入 NAS；建议先 dry_run 看计划。', '默认什么都不动。打开开关后才能真正修改 NAS 上的文件；建议先勾选「先看计划，不动文件」预览一遍。')));
  const gform = el('div', 'tool-form');
  refs.write = switchField(t('enable_write · 字幕下载 / 混流 / 整理执行', '允许下载字幕、把字幕合并进视频、整理移动文件'));
  refs.write.input.checked = !!cfg.enable_write;
  refs.organize = switchField(t('enable_organize · 媒体目录整理', '允许自动归类、移动文件'));
  refs.organize.input.checked = !!cfg.enable_organize;
  refs.delete = switchField(t('enable_delete · 垃圾清理删除', '允许删除文件（请谨慎）'));
  refs.delete.input.checked = !!cfg.enable_delete;
  for (const f of [refs.write, refs.organize, refs.delete]) gform.appendChild(f.field);
  gbody.appendChild(gform);
  gate.appendChild(gbody);
  view.appendChild(gate);

  const enabledSources = [cfg.tmdb.enabled, cfg.jackett.enabled, cfg.seedhub.enabled, cfg.dysbk.enabled].filter(Boolean).length;
  const srcGroup = configGroup(t('资源与数据源', '在线资源网站'),
    t(`已启用 ${enabledSources} / 4 个源`, `开了 ${enabledSources} / 4 个网站`), false);
  const sources = srcGroup.root;
  const obody = srcGroup.body;
  const oform = el('div', 'tool-form');
  refs.tmdbEnabled = switchField(t('TMDB 中文片名解析', '用 TMDB 识别中文片名'));
  refs.tmdbEnabled.input.checked = !!cfg.tmdb.enabled;
  refs.tmdbKey = secretField(t('TMDB API Key', 'TMDB 密钥（API Key）'), cfg.tmdb.api_key_configured);
  refs.jackettEnabled = switchField(t('Jackett 资源搜索', '用 Jackett 搜资源'));
  refs.jackettEnabled.input.checked = !!cfg.jackett.enabled;
  refs.jackettUrl = configField(t('Jackett 地址', 'Jackett 网址'), '例如 http://127.0.0.1:9117');
  refs.jackettUrl.input.value = cfg.jackett.url || '';
  refs.jackettKey = secretField(t('Jackett API Key', 'Jackett 密钥（API Key）'), cfg.jackett.api_key_configured);
  refs.seedhub = switchField('SeedHub（磁力 + 网盘）');
  refs.seedhub.input.checked = !!cfg.seedhub.enabled;
  refs.dysbk = switchField('网盘资源分享社 dysbk');
  refs.dysbk.input.checked = !!cfg.dysbk.enabled;
  for (const f of [refs.tmdbEnabled, refs.tmdbKey, refs.jackettEnabled, refs.jackettUrl, refs.jackettKey, refs.seedhub, refs.dysbk]) oform.appendChild(f.field);
  obody.appendChild(oform);
  sources.appendChild(obody);
  view.appendChild(sources);

  const enabledSubs = [cfg.subhd.enabled, cfg.opensubtitles.enabled, cfg.shooter.enabled].filter(Boolean).length;
  const subGroup = configGroup('字幕站点',
    t(`已启用 ${enabledSubs} / 3 个站点`, `开了 ${enabledSubs} / 3 个站点`), false);
  const subtitleCard = subGroup.root;
  const tbody = subGroup.body;
  const tform = el('div', 'tool-form');
  refs.subhd = switchField('SubHD');
  refs.subhd.input.checked = !!cfg.subhd.enabled;
  refs.os = switchField('OpenSubtitles');
  refs.os.input.checked = !!cfg.opensubtitles.enabled;
  refs.osKey = secretField(t('OpenSubtitles API Key', 'OpenSubtitles 密钥（API Key）'), cfg.opensubtitles.api_key_configured);
  refs.osUser = configField('OpenSubtitles 用户名',
    cfg.opensubtitles.username_configured ? '已配置（留空保持不变）' : '未配置');
  refs.osPass = secretField('OpenSubtitles 密码', cfg.opensubtitles.password_configured);
  refs.shooter = switchField('Shooter');
  refs.shooter.input.checked = !!cfg.shooter.enabled;
  refs.shooterToken = secretField(t('Shooter Token', 'Shooter 密钥（Token）'), cfg.shooter.token_configured);
  for (const f of [refs.subhd, refs.os, refs.osKey, refs.osUser, refs.osPass, refs.shooter, refs.shooterToken]) tform.appendChild(f.field);
  if (cfg.subhd.base_url) {
    tbody.appendChild(el('p', 'muted mono',
      t('SubHD 地址（暂不支持在界面修改）：' + cfg.subhd.base_url,
        'SubHD 网址（界面改不了，要改得编辑配置文件）：' + cfg.subhd.base_url)));
  }
  tbody.appendChild(tform);
  subtitleCard.appendChild(tbody);
  view.appendChild(subtitleCard);

  const cleanupGroup = configGroup(t('清理规则', '清理垃圾文件'),
    t(`${(cfg.cleanup.junk_extensions || []).length} 种垃圾扩展名`, `${(cfg.cleanup.junk_extensions || []).length} 种垃圾文件`), false);
  const cleanup = cleanupGroup.root;
  const cbody = cleanupGroup.body;
  const cform = el('div', 'tool-form');
  refs.junk = configArea(t('垃圾扩展名', '算作垃圾的文件类型'), t('每行一个，例如 .nfo .txt .url', '每行一个，比如 .nfo、.txt、.url'));
  refs.junk.input.value = (cfg.cleanup.junk_extensions || []).join('\n');
  refs.grace = configField(t('下载中宽限分钟数', '正在下载的文件保留几分钟（避免误删）'));
  refs.grace.input.type = 'number';
  refs.grace.input.value = String(cfg.cleanup.active_download_grace_minutes ?? 0);
  for (const f of [refs.junk, refs.grace]) cform.appendChild(f.field);
  cbody.appendChild(cform);
  cleanup.appendChild(cbody);
  view.appendChild(cleanup);

  // 后端 config_view 返回的 limits 是只读信息，展示出来，避免“后端有、界面看不见”
  const limits = cfg.limits || {};
  if (Object.keys(limits).length) {
    const lim = el('article', 'card');
    const lh = el('div', 'card-head');
    lh.appendChild(el('h2', null, t('运行限额（只读）', '运行限制（改不了）')));
    lim.appendChild(lh);
    const lb = el('div', 'card-body');
    for (const [k, v] of Object.entries(limits)) {
      const row = el('p', 'muted');
      row.appendChild(el('span', null, label(k) + '：'));
      row.appendChild(el('code', null, String(v)));
      lb.appendChild(row);
    }
    lb.appendChild(el('p', 'muted', t('这些限额来自 config.yaml 的 limits 段，界面只读，需要手动编辑配置文件修改。',
      '这些限制写在配置文件里，界面上只能看不能改。')));
    lim.appendChild(lb);
    view.appendChild(lim);
  }

  const actions = el('div', 'form-actions');
  const save = el('button', 'primary', t('保存配置', '保存'));
  save.type = 'button';
  const reload = el('button', 'ghost', t('重新载入', '放弃修改，重新读取'));
  reload.type = 'button';
  const dirty = el('span', 'badge warn', t('有未保存的修改', '改了还没保存'));
  dirty.hidden = true;
  actions.appendChild(dirty);
  if (IS_PRO()) actions.appendChild(el('span', 'note mono', cfg.config_path || ''));
  actions.appendChild(reload);
  actions.appendChild(save);
  form.appendChild(actions);
  const saveNote = el('div', 'save-ok');
  saveNote.hidden = true;
  view.appendChild(form);
  view.appendChild(saveNote);

  const markDirty = () => { dirty.hidden = false; saveNote.hidden = true; S.configDirty = true; };
  view.addEventListener('input', markDirty);
  view.addEventListener('change', markDirty);

  const out = el('div');
  view.appendChild(out);

  reload.addEventListener('click', async () => {
    if (S.configDirty) {
      const ok = await confirmDialog({
        title: t('放弃这些修改？', '这些修改不要了？'),
        intro: t('重新载入会丢掉尚未保存的改动。', '重新读取会把还没保存的改动清掉。'),
        confirmText: t('放弃修改', '不要了'),
        danger: true,
      });
      if (!ok) return;
    }
    S.configDirty = false;
    route();
  });
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const updates = {
        allowed_paths: lines(refs.allowed.input.value),
        media_roots: lines(refs.media.input.value),
        exclude_paths: lines(refs.exclude.input.value),
        enable_write: refs.write.input.checked,
        enable_organize: refs.organize.input.checked,
        enable_delete: refs.delete.input.checked,
        tmdb: { enabled: refs.tmdbEnabled.input.checked },
        jackett: { enabled: refs.jackettEnabled.input.checked, url: refs.jackettUrl.input.value.trim() },
        subhd: { enabled: refs.subhd.input.checked },
        opensubtitles: { enabled: refs.os.input.checked },
        shooter: { enabled: refs.shooter.input.checked },
        seedhub: { enabled: refs.seedhub.input.checked },
        dysbk: { enabled: refs.dysbk.input.checked },
        cleanup: {
          junk_extensions: lines(refs.junk.input.value),
          active_download_grace_minutes: parseInt(refs.grace.input.value || '0', 10),
        },
      };
      if (refs.tmdbKey.input.value.trim()) updates.tmdb.api_key = refs.tmdbKey.input.value.trim();
      if (refs.jackettKey.input.value.trim()) updates.jackett.api_key = refs.jackettKey.input.value.trim();
      if (refs.osKey.input.value.trim()) updates.opensubtitles.api_key = refs.osKey.input.value.trim();
      // 用户名界面不回显明文：只有新填了值才提交，留空保持原值
      if (refs.osUser.input.value.trim()) updates.opensubtitles.username = refs.osUser.input.value.trim();
      if (refs.osPass.input.value.trim()) updates.opensubtitles.password = refs.osPass.input.value.trim();
      if (refs.shooterToken.input.value.trim()) updates.shooter.token = refs.shooterToken.input.value.trim();
      const items = cfgDiffItems(cfg, updates);
      if (items.length) {
        const openingDelete = !cfg.enable_delete && updates.enable_delete;
        const ok = await confirmDialog({
          title: t('确认保存这些修改？', '确认现在就保存吗？'),
          intro: t('保存后立即生效；以下是你即将做的改动。', '保存后马上生效，下面这些就是这次要改的东西。'),
          items,
          confirmText: t('保存', '保存'),
          danger: items.some((it) => it.danger),
          requireWord: openingDelete ? t('删除', '删除') : '',
        });
        if (!ok) return;
      }
      const result = await api('/api/config', { method: 'PUT', body: updates });
      S.config = result;
      S.configDirty = false;
      dirty.hidden = true;
      setExpandedText(result.allowed_paths_expanded);
      // 密钥输入框清空，占位符改为「已配置」，下次留空即保持原值
      for (const [key, conf] of [
        [refs.tmdbKey, result.tmdb.api_key_configured],
        [refs.jackettKey, result.jackett.api_key_configured],
        [refs.osKey, result.opensubtitles.api_key_configured],
        [refs.shooterToken, result.shooter.token_configured],
      ]) {
        key.input.value = '';
        key.input.placeholder = conf ? '已配置（留空保持不变）' : '未配置';
      }
      const srcCount = [result.tmdb.enabled, result.jackett.enabled, result.seedhub.enabled, result.dysbk.enabled].filter(Boolean).length;
      const subCount = [result.subhd.enabled, result.opensubtitles.enabled, result.shooter.enabled].filter(Boolean).length;
      srcGroup.hint.textContent = t(`已启用 ${srcCount} / 4 个源`, `开了 ${srcCount} / 4 个网站`);
      subGroup.hint.textContent = t(`已启用 ${subCount} / 3 个站点`, `开了 ${subCount} / 3 个站点`);
      cleanupGroup.hint.textContent = t(`${(result.cleanup.junk_extensions || []).length} 种垃圾扩展名`,
        `${(result.cleanup.junk_extensions || []).length} 种垃圾文件`);
      saveNote.textContent = '';
      saveNote.appendChild(el('strong', null, t('已保存并立即生效', '保存好了，马上生效')));
      saveNote.appendChild(el('span', null,
        items.length
          ? t(`本次共 ${items.length} 项改动，无需重启，新设置已用于后续任务。`, `这次改了 ${items.length} 项，不用重启，之后的任务就用新设置。`)
          : t('没有改动，当前设置已确认生效。', '没有改动，当前设置就是生效状态。')));
      saveNote.hidden = false;
      toast(t('配置已保存并生效', '保存好了，马上生效'));
    } catch (err) {
      out.textContent = '';
      out.appendChild(errorBox({ code: err.code || 'save_failed', message: err.message }));
    } finally {
      save.disabled = false;
    }
  });
}

// ---------------------------------------------------------------- 工具台
async function pageWorkbench(view) {
  setHeader('工具台', t(`全部 ${Object.keys(S.tools).length} 个工具、后台任务与日志。`, `一共 ${Object.keys(S.tools).length} 个功能，还有后台任务和日志。`));
  const toolsCard = el('article', 'card');
  const head = el('div', 'card-head');
  head.appendChild(el('h2', null, t('全部工具', '所有功能')));
  toolsCard.appendChild(head);
  const body = el('div', 'card-body');
  const toolbar = el('div', 'toolbar');
  const search = document.createElement('input');
  search.type = 'text';
  search.className = 'page-filter';
  search.placeholder = t('按名称 / 标题 / 用途筛选', '找功能：试试「字幕」或「整理」');
  search.value = S.filter;
  const countSpan = el('span', 'muted', '');
  toolbar.append(search, countSpan);
  body.appendChild(toolbar);
  const list = el('div', 'tool-groups');
  body.appendChild(list);
  toolsCard.appendChild(body);
  view.appendChild(toolsCard);

  let storedOpen = loadWorkbenchOpen();
  let searchOpen = {};
  let lastQ = null;
  let openName = null;
  const buttons = new Map();
  const hosts = new Map();

  // 收起当前展开的卡片；关闭后把焦点还给原来那张工具卡，页面位置自然回到原处
  const closeOpenCard = (focusBtn) => {
    for (const host of hosts.values()) { host.textContent = ''; host.hidden = true; }
    openName = null;
    if (focusBtn) focusBtn.focus();
  };

  const openToolCard = (name, btn) => {
    if (openName === name) { closeOpenCard(btn); return; }
    closeOpenCard(null);
    const host = hosts.get(name);
    if (!host) return;
    openName = name;
    host.hidden = false;
    host.textContent = '';
    const bar = el('div', 'tool-open-bar');
    bar.appendChild(el('span', 'muted', t('已展开，填好参数后执行', '展开了，填好就可以开始')));
    const close = el('button', 'ghost small', '收起');
    close.type = 'button';
    close.addEventListener('click', () => closeOpenCard(btn));
    bar.appendChild(close);
    host.append(bar, toolCard(name));
  };

  const draw = () => {
    list.textContent = '';
    buttons.clear();
    hosts.clear();
    openName = null;
    const q = S.filter.trim().toLowerCase();
    if (q !== lastQ) { searchOpen = {}; lastQ = q; }
    let shown = 0;
    const allToolNames = new Set(Object.keys(S.tools));
    const groups = WORKBENCH_GROUPS.slice();
    // 兜底：任何未归入分组的工具仍然出现在工具台，避免升级后新工具「消失」
    const known = new Set(groups.flatMap((g) => g.tools));
    const leftovers = [...allToolNames].filter((n) => !known.has(n));
    if (leftovers.length) groups.push({ id: 'other', title: '其他', hint: '未归类的功能', tools: leftovers });

    for (const group of groups) {
      const names = group.tools.filter((n) => S.tools[n]);
      const matched = names.filter((n) => {
        if (!q) return true;
        const spec = S.tools[n];
        const hay = (n + ' ' + spec.title + ' ' + spec.category + ' ' + pick(FRIENDLY.tool, n, '') + ' ' + toolSummary(n, spec)).toLowerCase();
        return hay.includes(q);
      });
      if (q && !matched.length) continue;
      shown += matched.length;

      const section = el('section', 'tool-group');
      const headBtn = el('button', 'tool-group-head');
      headBtn.type = 'button';
      const caret = el('span', 'tg-caret');
      caret.appendChild(icon('chevron'));
      headBtn.append(caret, el('strong', null, group.title), el('span', 'muted', matched.length + ' 个功能'));
      headBtn.title = group.hint || '';
      const groupBody = el('div', 'tool-group-body');
      const grid = el('div', 'tool-list');
      groupBody.appendChild(grid);
      const painter = () => {
        const open = q ? searchOpen[group.id] !== false : storedOpen[group.id] !== false;
        headBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        groupBody.hidden = !open;
        caret.classList.toggle('open', open);
      };
      headBtn.addEventListener('click', () => {
        const open = q ? searchOpen[group.id] !== false : storedOpen[group.id] !== false;
        if (q) searchOpen[group.id] = !open;
        else { storedOpen[group.id] = !open; saveWorkbenchOpen(storedOpen); }
        painter();
      });

      for (const name of matched) {
        const spec = S.tools[name];
        const btn = el('button', 'tool-item');
        btn.type = 'button';
        btn.appendChild(el('strong', null, pick(FRIENDLY.tool, name, spec.title)));
        const oneLine = toolSummary(name, spec);
        if (oneLine) btn.appendChild(el('span', 'tool-desc', oneLine));
        const badges = el('div', 'badges');
        badges.appendChild(el('span', 'badge risk ' + spec.risk, pick(FRIENDLY.risk, spec.risk, spec.risk_label)));
        if (spec.long_running) badges.appendChild(el('span', 'badge', t('后台', '慢一点')));
        btn.appendChild(badges);
        buttons.set(name, btn);

        const host = el('div', 'tool-open-host');
        host.hidden = true;
        hosts.set(name, host);
        grid.appendChild(btn);
        groupBody.appendChild(host);
        btn.addEventListener('click', () => openToolCard(name, btn));
      }
      section.append(headBtn, groupBody);
      painter();
      list.appendChild(section);
    }

    countSpan.textContent = q
      ? t(`显示 ${shown} / ${allToolNames.size} 个工具`, `找到 ${shown} / ${allToolNames.size} 个功能`)
      : t(`${allToolNames.size} 个工具`, `${allToolNames.size} 个功能`);
    if (!shown) {
      list.appendChild(emptyState({
        title: t('没有匹配的工具。', '没有找到匹配的功能'),
        hint: t('换个关键词，或用右上角的「搜索工具」直接找。', '换个词试试，也可以点上面的搜索按钮'),
        action: {
          label: t('清空搜索', '清空搜索'),
          onClick: () => { S.filter = ''; search.value = ''; draw(); search.focus(); },
        },
      }));
    }
  };
  search.addEventListener('input', () => { S.filter = search.value; draw(); });
  draw();

  // 接力/深链落到工具台时，直接展开目标工具卡（分类页覆盖不到的工具走这里）
  if (S.pending && S.tools[S.pending.tool] && !NAV.find((n) => (n.tools || []).includes(S.pending.tool))) {
    const name = S.pending.tool;
    const group = WORKBENCH_GROUPS.find((g) => g.tools.includes(name));
    if (group) {
      const q = S.filter.trim().toLowerCase();
      if (q) searchOpen[group.id] = true; else { storedOpen[group.id] = true; saveWorkbenchOpen(storedOpen); }
      draw();
    }
    const btn = buttons.get(name);
    if (btn) openToolCard(name, btn);
  }

  const jobsCard = el('article', 'card');
  const jh = el('div', 'card-head');
  jh.appendChild(el('h2', null, t('后台任务', '后台任务')));
  const jobsTools = el('div', 'toolbar');
  const statusSel = document.createElement('select');
  statusSel.setAttribute('aria-label', t('按状态筛选任务', '按状态筛选'));
  for (const [v, txt] of [['', t('全部状态', '全部')], ['running', t('进行中', '正在跑')], ['queued', t('排队中', '排队中')], ['succeeded', t('已完成', '已完成')], ['failed', t('失败', '失败')], ['canceled', t('已取消', '已取消')], ['interrupted', t('已中断', '已中断')]]) {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = txt;
    statusSel.appendChild(opt);
  }
  const refreshJobs = el('button', 'ghost small', '刷新');
  refreshJobs.type = 'button';
  jobsTools.append(statusSel, refreshJobs);
  jh.appendChild(jobsTools);
  jobsCard.appendChild(jh);
  const jobsBodyWrap = el('div', 'card-body');
  jobsBodyWrap.appendChild(el('p', 'muted jobs-note',
    t('任务在服务器上继续执行，关掉页面或切到别的页面都不会停；进行中的任务可以在这里取消，已完成的部分会保留。',
      '任务在后台跑，关掉页面也不影响；正在跑的任务可以取消，做完的部分会保留。')));
  const jbody = el('div');
  jobsBodyWrap.appendChild(jbody);
  // 结果位放在任务列表下方：点「查看结果」只把结果插到这里，列表位置不变
  const jobHost = el('div');
  jobHost.id = 'job-detail-host';
  jobsBodyWrap.appendChild(jobHost);
  jobsCard.appendChild(jobsBodyWrap);
  view.appendChild(jobsCard);

  let jobsCache = [];
  let lastJobsSig = '';
  const paintJobs = () => {
    const list = statusSel.value ? jobsCache.filter((j) => j.status === statusSel.value) : jobsCache;
    jbody.textContent = '';
    if (!list.length) {
      const filtered = jobsCache.length > 0;
      jbody.appendChild(emptyState({
        title: filtered ? t('没有符合这个状态的任务。', '这个状态下没有任务') : t('暂无任务。', '这里还没有任务'),
        hint: filtered ? t('切换到「全部状态」查看所有任务。', '切回「全部」就能看到全部任务') : t('到分类页执行一个耗时功能，任务会出现在这里。', '去上面的功能里做一件事，慢的会出现在这里。'),
        action: filtered
          ? { label: t('看全部任务', '看全部任务'), onClick: () => { statusSel.value = ''; paintJobs(); } }
          : { label: t('去扫描媒体库', '去做一件事'), onClick: () => { location.hash = '#/scan'; } },
      }));
      return;
    }
    jbody.appendChild(renderJobs(list, paintJobs));
  };
  const loadJobs = async (force = false) => {
    // 后台标签页不轮询；但首次进入必须拉一次，否则隐藏状态打开会误显示「暂无任务」
    if (document.hidden && !force) return;
    try {
      const data = await api('/api/jobs?limit=40');
      jobsCache = data.jobs || [];
      const sig = JSON.stringify(jobsCache.map((j) => [j.id, j.status, j.finished_at, j.cancel_requested, j.progress]));
      if (sig !== lastJobsSig) { lastJobsSig = sig; paintJobs(); }
    } catch (err) {
      jbody.textContent = '';
      jbody.appendChild(errorBox({ code: err.code || 'load_failed', message: err.message }, { retry: loadJobs }));
    }
  };
  statusSel.addEventListener('change', paintJobs);
  refreshJobs.addEventListener('click', loadJobs);
  await loadJobs(true);
  if (S.jobsVisibleHandler) document.removeEventListener('visibilitychange', S.jobsVisibleHandler);
  S.jobsVisibleHandler = () => { if (!document.hidden) loadJobs(true); };
  document.addEventListener('visibilitychange', S.jobsVisibleHandler);
  S.timer = setInterval(loadJobs, 2000);

  const logCard = el('article', 'card');
  const lh = el('div', 'card-head');
  lh.appendChild(el('h2', null, '日志'));
  logCard.appendChild(lh);
  const lbody = el('div', 'card-body');
  const ltoolbar = el('div', 'toolbar');
  const select = document.createElement('select');
  for (const [value, text] of [['app', '应用日志'], ['audit', '整理审计日志']]) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = text;
    select.appendChild(opt);
  }
  const linesInput = document.createElement('input');
  linesInput.type = 'number';
  linesInput.value = '200';
  linesInput.min = '1';
  linesInput.max = '2000';
  const loadLogs = el('button', 'ghost small', '读取');
  loadLogs.type = 'button';
  ltoolbar.append(select, linesInput, loadLogs);
  lbody.appendChild(ltoolbar);
  const pre = el('pre', 'json', '点击「读取」查看最近日志。');
  lbody.appendChild(pre);
  logCard.appendChild(lbody);
  view.appendChild(logCard);

  loadLogs.addEventListener('click', async () => {
    pre.textContent = '读取中…';
    try {
      const data = await api(`/api/logs?file=${select.value}&lines=${encodeURIComponent(linesInput.value)}`);
      pre.textContent = data.message || (data.lines && data.lines.length ? data.lines.join('\n') : '（空）');
      pre.textContent += '\n\n# ' + (data.file || '');
    } catch (err) {
      pre.textContent = '读取失败：' + err.message;
    }
  });
}

// ---------------------------------------------------------------- 全局任务指示器
async function pollJobsIndicator() {
  const ind = document.getElementById('jobs-ind');
  if (!ind || !S.meta) return;
  try {
    const data = await api('/api/jobs?limit=30');
    const jobs = data.jobs || [];
    const active = jobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
    if (!active) { ind.hidden = true; return; }
    ind.hidden = false;
    const jobsLabel = document.getElementById('jobs-label');
    const jobsText = t(`${active} 个任务进行中`, `${active} 个任务进行中`);
    if (jobsLabel) jobsLabel.textContent = jobsText;
    else ind.textContent = jobsText;
    ind.title = t('点击查看后台任务', '点击查看后台任务');
  } catch (_) {
    /* 指示器失败不影响页面 */
  }
}

// ---------------------------------------------------------------- 启动
// 顶栏健康徽标：真实探测 /healthz（server.py），而不是写死显示应用名
async function refreshHealth() {
  const badge = document.getElementById('health');
  if (!badge) return;
  try {
    const res = await fetch('/healthz', { credentials: 'same-origin' });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error('healthz failed');
    badge.className = 'badge ok';
    badge.textContent = '';
    badge.appendChild(icon('check'));
    badge.appendChild(document.createTextNode(t(`服务正常 · v${data.version || ''}`, `服务正常 v${data.version || ''}`)));
  } catch (_) {
    badge.className = 'badge err';
    badge.textContent = '';
    badge.appendChild(icon('alert'));
    badge.appendChild(document.createTextNode(t('服务异常', '服务连不上')));
  }
}

async function boot() {
  mountIcons();
  try {
    S.meta = await api('/api/meta');
  } catch (err) {
    if (!/登录已失效/.test(err.message)) {
      document.getElementById('boot').textContent = '连接失败：' + err.message;
    }
    return;
  }
  S.tools = Object.fromEntries((S.meta.tools || []).map((t) => [t.name, t]));
  try {
    S.config = await api('/api/config');
  } catch (_) {
    S.config = null;
  }
  document.getElementById('brand-version').textContent =
    `应用 ${S.meta.app.version} · 核心 ${S.meta.app.core_version}`;
  applyShellCopy();
  loadRecentPaths();
  refreshHealth();
  setInterval(refreshHealth, 60000);

  buildNav();
  document.getElementById('logout-btn').addEventListener('click', async () => {
    try { await api('/logout', { method: 'POST' }); } catch (_) { /* ignore */ }
    location.href = '/login';
  });
  let leavePending = null;
  const canLeaveConfig = () => {
    if (!S.configDirty) return Promise.resolve(true);
    if (leavePending) return leavePending;
    leavePending = confirmDialog({
      title: t('配置有未保存的修改', '配置还没保存'),
      intro: t('离开会丢失这些修改，确定离开吗？', '现在走的话，刚才改的就都没了。'),
      cancelText: t('继续编辑', '继续编辑'),
      confirmText: t('放弃修改', '放弃修改'),
      hideClose: true,
      danger: true,
    }).then((ok) => {
      leavePending = null;
      if (ok) S.configDirty = false;
      return ok;
    });
    return leavePending;
  };
  let lastHash = location.hash || '#/overview';
  document.getElementById('refresh-btn').addEventListener('click', async () => {
    if (!(await canLeaveConfig())) return;
    route();
  });
  document.getElementById('theme-btn').addEventListener('click', cycleTheme);
  const tsBtn = document.getElementById('tool-search-btn');
  if (tsBtn) tsBtn.addEventListener('click', openToolSearch);
  document.addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'k' || ev.key === 'K')) {
      ev.preventDefault();
      openToolSearch();
    } else if (ev.key === '/' && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
      // 与常见列表页一致：按 / 直接聚焦当前页的筛选框；正在输入时不抢焦点
      const ae = document.activeElement;
      if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT' || ae.isContentEditable)) return;
      const box = document.querySelector('.table-filter input[type="search"]');
      if (box) {
        ev.preventDefault();
        box.focus();
        if (typeof box.select === 'function') box.select();
      }
    } else if (ev.key === 'Escape') {
      const mask = document.getElementById('tool-search');
      if (mask) mask.remove();
    }
  });
  const jobsInd = document.getElementById('jobs-ind');
  jobsInd.addEventListener('click', () => { location.hash = '#/workbench'; });
  window.addEventListener('hashchange', async () => {
    const next = location.hash || '#/overview';
    if (!(await canLeaveConfig())) {
      history.replaceState(null, '', lastHash);
      return;
    }
    lastHash = next;
    route();
  });
  window.addEventListener('beforeunload', (ev) => {
    if (S.configDirty) { ev.preventDefault(); ev.returnValue = ''; }
  });
  document.getElementById('boot').hidden = true;
  document.getElementById('app').hidden = false;
  setupNavDrawer();
  applyTheme();
  if (themeMq && themeMq.addEventListener) {
    themeMq.addEventListener('change', () => { if (themePref() === 'auto') applyTheme(); });
  }
  await route();
  pollJobsIndicator();
  S.jobTimer = setInterval(pollJobsIndicator, 8000);
  setInterval(tickJobElapsed, 1000);
}


boot();
