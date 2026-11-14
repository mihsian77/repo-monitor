// repo-monitor 可选实时接收端（Cloudflare Worker / 阶段 1）
//
// 部署方式：把本文件放到 Worker，配置 GitHub App 的 Webhook URL 指向它。
// App 装在账号上，一次配置覆盖所有仓库；各仓库 Settings→Webhooks 保持空白，
// 不产生任何仓库内容或提交。本 Worker 不调用任何 GitHub 写接口。
//
// Secrets（Worker 环境变量）：
//   FEISHU_WEBHOOK   飞书机器人地址（重置后的新地址）
//   WEBHOOK_SECRET   GitHub App Webhook 的签名密钥
//   KV               可选：命名空间绑定，用于幂等与心跳（不绑定则退化为内存去重）
//
// 订阅事件建议（App 设置页只勾这些，减少投递量）：
//   push, issues, issue_comment, pull_request, pull_request_review,
//   pull_request_review_comment, release, workflow_run, check_run,
//   dependabot_alert, secret_scanning_alert, code_scanning_alert,
//   branch_protection_rule, create, delete, fork, star, watch

const P0_EVENTS = new Set([
  'issues', 'issue_comment', 'pull_request_review', 'pull_request_review_comment',
  'workflow_run', 'check_run', 'dependabot_alert', 'secret_scanning_alert',
  'code_scanning_alert', 'branch_protection_rule',
]);

const BOT_SUFFIX = '[bot]';

function e0Repo(p) {
  return p?.repository?.full_name || '?';
}

function isBot(login) {
  return !login || login.toLowerCase().endsWith(BOT_SUFFIX) ||
    login.toLowerCase().includes('coderabbit') ||
    login.toLowerCase().includes('dependabot');
}

function normalize(eventName, p) {
  const repo = p.repository?.full_name || '?';
  const actor = p.sender?.login || '?';
  const base = { repo, actor, event: eventName, title: '', url: '', body: '' };
  switch (eventName) {
    case 'push': {
      const commits = p.commits || [];
      const files = [];
      for (const c of commits) {
        files.push(...(c.added || []), ...(c.modified || []), ...(c.removed || []));
      }
      return { ...base, kind: 'push', title: (commits.at(-1)?.message || '').split('\n')[0],
               url: p.compare || '', files: [...new Set(files)], pusher: p.pusher?.name || actor };
    }
    case 'issues':
      return { ...base, kind: 'issue', title: p.issue?.title || '', url: p.issue?.html_url || '',
               body: (p.issue?.body || '').slice(0, 500), action: p.action };
    case 'issue_comment':
      return { ...base, kind: 'comment', url: p.comment?.html_url || '',
               body: (p.comment?.body || '').slice(0, 500) };
    case 'pull_request':
    case 'pull_request_review':
    case 'pull_request_review_comment': {
      const obj = p.pull_request || p.review || p.comment || {};
      return { ...base, kind: 'pr', title: obj.title || '', url: obj.html_url || '',
               body: (obj.body || '').slice(0, 500), action: p.action };
    }
    case 'release':
      return { ...base, kind: 'release', title: p.release?.name || p.release?.tag_name || '',
               url: p.release?.html_url || '' };
    case 'workflow_run':
    case 'check_run': {
      const r = p.workflow_run || p.check_run || {};
      return { ...base, kind: r.conclusion === 'failure' ? 'run_failure' : 'run_other',
               title: r.name || '', url: r.html_url || '', conclusion: r.conclusion };
    }
    default:
      return { ...base, kind: eventName };
  }
}

function decide(e, selfRepo) {
  // 与 src/rules.py 保持同一套判定思路：真人强信号、机器人压成日报、
  // 管家自身 CI 静默（由工作流层 P1 + 12 小时心跳卡独立覆盖）。
  if (['dependabot_alert', 'secret_scanning_alert', 'code_scanning_alert',
       'branch_protection_rule'].includes(e.event)) {
    return { level: 'P0', reason: '安全告警 / 保护规则变更' };
  }
  if (e.kind === 'run_failure') {
    // 自身 CI 事件已在 fetch 入口按 selfRepo 整体忽略；这里再兜一层，
    // 避免任何漏进来的自身失败又变成一条「管家自身故障」P0 红卡。
    if (e.repo === selfRepo) return { level: 'DROP', reason: '管家自身构建，交由工作流层覆盖' };
    return { level: 'P0', reason: '真实构建失败' };
  }
  if (isBot(e.actor)) {
    if (e.kind === 'comment' && /auto-generated comment|coderabbit/i.test(e.body || '')) {
      return { level: 'DROP', reason: '机器人模板评论，静默' };
    }
    return { level: 'P2', reason: '机器人事件，并入日报' };
  }
  if (['issue', 'comment'].includes(e.kind)) {
    return { level: 'P0', reason: '外部真人留言 / 新建 Issue' };
  }
  if (e.kind === 'push') {
    const sensitive = (e.files || []).some(f => /\.(c|cc|cpp|h|hpp)$|CMakeLists\.txt$|Android\.mk$|\.mk$|build\.gradle|\/jni\/|\/native\//i.test(f));
    return sensitive
      ? { level: 'P1', reason: '人工关键提交命中敏感模块',
          advice: '改动命中 C/C++ 或构建/下载引擎：合并前先对 diff，C 代码务必真机验证。' }
      : { level: 'P2', reason: '人工普通提交，并入日报' };
  }
  return { level: 'P2', reason: '例行事件' };
}

// 飞书机器人「签名校验」：sign = base64(HMAC-SHA256(key=`ts\nsecret`, msg=b""))
async function feishuSign(timestamp, secret) {
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(`${timestamp}\n${secret}`),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = await crypto.subtle.sign('HMAC', key, new Uint8Array());
  return btoa(String.fromCharCode(...new Uint8Array(mac)));
}

async function pushFeishu(env, title, lines, level) {
  if (!env.FEISHU_WEBHOOK) return false;
  const payload = {
    msg_type: 'post',
    content: { post: { zh_cn: {
      title: level === 'P0' ? `【P0】${title}` : title,
      content: lines.filter(Boolean).map(t => [{ tag: 'text', text: t }]),
    } } },
  };
  // 配置了 FEISHU_SECRET 才附加签名；留空表示机器人未开启签名校验（向后兼容）
  if (env.FEISHU_SECRET) {
    const ts = String(Math.floor(Date.now() / 1000));
    payload.timestamp = ts;
    payload.sign = await feishuSign(ts, env.FEISHU_SECRET);
  }
  const r = await fetch(env.FEISHU_WEBHOOK, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return r.ok;
}

async function verifySignature(request, rawBody, secret) {
  if (!secret) return true; // 未配置时放行，但会在日志里提示
  const sig = request.headers.get('X-Hub-Signature-256') || '';
  if (!sig.startsWith('sha256=')) return false;
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(rawBody));
  const expected = 'sha256=' + [...new Uint8Array(mac)]
    .map(b => b.toString(16).padStart(2, '0')).join('');
  // 定长比较，避免时序侧信道
  if (sig.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < sig.length; i++) diff |= sig.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

async function alreadySeen(env, id) {
  if (!env.KV) return false;
  return (await env.KV.get(`delivery:${id}`)) !== null;
}

async function markSeen(env, id) {
  if (!env.KV) return;
  await env.KV.put(`delivery:${id}`, '1', { expirationTtl: 60 * 60 * 24 * 7 });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('repo-monitor webhook endpoint', { status: 200 });
    }
    const raw = await request.text();
    if (!(await verifySignature(request, raw, env.WEBHOOK_SECRET))) {
      return new Response('invalid signature', { status: 401 });
    }
    const eventName = request.headers.get('X-GitHub-Event') || '';
    const deliveryId = request.headers.get('X-GitHub-Delivery') || '';
    let payload;
    try { payload = JSON.parse(raw); } catch { return new Response('bad json', { status: 400 }); }
    if (eventName === 'ping') return new Response('pong', { status: 200 });

    // X-GitHub-Delivery 全局唯一，天然幂等键
    if (deliveryId && await alreadySeen(env, deliveryId)) {
      return new Response('duplicate, ignored', { status: 200 });
    }

    const selfRepo = env.SELF_REPO || 'your-org/repo-monitor';
    // 管家自身 CI 的运行事件不在这里告警：它会把自己变成一条
    // 「管家自身故障」P0 卡，与工作流层的失败告警、pipeline 里的 12 小时
    // 心跳卡重复，正是「无用通知」的来源。自身健康由那两处独立覆盖。
    if ((eventName === 'workflow_run' || eventName === 'check_run'
         || eventName === 'workflow_job') && e0Repo(payload) === selfRepo) {
      if (deliveryId) ctx.waitUntil(markSeen(env, deliveryId));
      return new Response('self-ci ignored', { status: 200 });
    }

    // 心跳在「每次成功处理投递」时更新，而不是只在 P0 时写：
    // 否则长期没有 P0 事件时 KV 里没有心跳，死人开关会必然误报。
    if (env.KV) ctx.waitUntil(env.KV.put('heartbeat:last', new Date().toISOString()));

    const e = normalize(eventName, payload);
    const d = decide(e, selfRepo);

    if (d.level === 'DROP') {
      if (deliveryId) ctx.waitUntil(markSeen(env, deliveryId));
      return new Response('silenced', { status: 200 });
    }

    const lines = [
      `仓库：${e.repo}`,
      `事件：${eventName} / ${e.actor}`,
      `判定：${d.reason}`,
      e.title ? `标题：${e.title}` : '',
      e.body ? `内容：${e.body.slice(0, 200)}` : '',
      e.url ? `链接：${e.url}` : '',
      d.advice ? `建议：${d.advice}` : '',
      e.files?.length ? `涉及文件：${e.files.slice(0, 5).join(', ')}` : '',
    ];

    if (d.level === 'P0') {
      await pushFeishu(env, `${e.repo.split('/').pop()} · ${e.kind}`, lines, 'P0');
    } else {
      // P1/P2 不逐条推：写入 KV 由对账巡检聚合，避免刷屏
      if (env.KV) {
        const key = `digest:${new Date().toISOString().slice(0, 10)}`;
        const prev = JSON.parse(await env.KV.get(key) || '[]');
        prev.push({ ts: new Date().toISOString(), level: d.level, repo: e.repo,
                    kind: e.kind, title: e.title, reason: d.reason });
        await env.KV.put(key, JSON.stringify(prev.slice(-500)), { expirationTtl: 60 * 60 * 24 * 7 });
      }
    }
    if (deliveryId) ctx.waitUntil(markSeen(env, deliveryId));
    return new Response(JSON.stringify({ level: d.level, kind: e.kind }),
                        { status: 200, headers: { 'Content-Type': 'application/json' } });
  },

  // 死人开关：外部 Cron 定时调用，专治「根本没运行」。
  // 与主链路 src/report.py:heartbeat_card 保持一致：阈值 12 小时、按天去重、
  // 中性标题；没有 KV 时无从判断，直接跳过，绝不靠「没有记录」误报。
  async scheduled(controller, env, ctx) {
    if (!env.KV) return new Response('kv-unbound, skip', { status: 200 });
    const last = await env.KV.get('heartbeat:last');
    if (!last) return new Response('no-heartbeat-yet, skip', { status: 200 });
    const maxMin = Number(env.HEARTBEAT_MAX_MINUTES || 720);
    const stale = (Date.now() - new Date(last).getTime()) > maxMin * 60 * 1000;
    if (!stale) return new Response('ok', { status: 200 });
    const day = new Date().toISOString().slice(0, 10);
    const alertKey = `heartbeat:alert:${day}`;
    if (await env.KV.get(alertKey)) return new Response('already-alerted-today', { status: 200 });
    const hours = Math.round(maxMin / 60);
    await pushFeishu(env, `【P0】实时接收端超过 ${hours} 小时未收到投递`, [
      `上次心跳：${last}`,
      `当前：${new Date().toISOString()}`,
      '飞书安静不代表没有事件，请检查 GitHub App Webhook 投递与 Worker 是否在运行。',
      '（正常巡检由 GitHub Actions 独立负责，两者互不影响）',
    ], 'P0');
    await env.KV.put(alertKey, '1', { expirationTtl: 60 * 60 * 24 * 2 });
    return new Response('stale-alert-sent', { status: 200 });
  },
};
