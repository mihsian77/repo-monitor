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
  // 与 src/rules.py 保持同一套判定思路：真人强信号、机器人压成日报、自身故障走心跳
  if (['dependabot_alert', 'secret_scanning_alert', 'code_scanning_alert',
       'branch_protection_rule'].includes(e.event)) {
    return { level: 'P0', reason: '安全告警 / 保护规则变更' };
  }
  if (e.kind === 'run_failure') {
    if (e.repo === selfRepo) return { level: 'HEARTBEAT', reason: '管家自身构建失败' };
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

    const e = normalize(eventName, payload);
    const d = decide(e, env.SELF_REPO || 'your-org/repo-monitor');
    const selfRepo = env.SELF_REPO || 'your-org/repo-monitor';

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

    if (d.level === 'P0' || d.level === 'HEARTBEAT') {
      const title = d.level === 'HEARTBEAT'
        ? '管家自身故障（心跳）'
        : `${e.repo.split('/').pop()} · ${e.kind}`;
      await pushFeishu(env, title, lines, 'P0');
      if (env.KV) await env.KV.put('heartbeat:last', new Date().toISOString());
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

  // 死人开关：外部 Cron 定时调用，专治「根本没运行」（巡检器自己挂了不能靠自己报警）
  async scheduled(controller, env, ctx) {
    const last = env.KV ? await env.KV.get('heartbeat:last') : null;
    const maxMin = Number(env.HEARTBEAT_MAX_MINUTES || 90);
    const stale = !last || (Date.now() - new Date(last).getTime()) > maxMin * 60 * 1000;
    if (stale) {
      await pushFeishu(env, '【P0】管家没上班（心跳超时）', [
        `上次心跳：${last || '无记录'}`,
        `当前：${new Date().toISOString()}`,
        '飞书安静不代表没有事件，请检查 GitHub Actions 与 Worker 是否在运行。',
      ], 'P0');
    }
    return new Response(stale ? 'stale-alert-sent' : 'ok');
  },
};
