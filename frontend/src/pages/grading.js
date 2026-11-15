import{store}from'../stores/business.js';
import{esc,num}from'../utils/format.js';
import{table}from'../components/common/table.js';
import{notice}from'../components/common/notice.js';
import{BATCH_STATUS,RESOLUTION,CONTRIBUTION_STATUS,SCORE_SOURCE}from'../constants/enums.js';

const batchOf=()=>store.grading?.batch||null;
const canEdit=()=>store.user&&store.user.role!=='viewer';
const isAdmin=()=>store.user&&store.user.role==='admin';

function statusBadge(batch){
  if(!batch)return '<span class="badge queued">尚未建立批次</span>';
  return `<span class="badge ${batch.status==='published'?'completed':batch.status==='ready'?'running':'queued'}">${BATCH_STATUS[batch.status]||batch.status}</span>`;
}

function conflictCount(batch){return (batch?.conflicts||[]).length;}

function rulesTable(questions){
  return table(['题号','满分','标准答案','状态','勘误'],(questions||[]).map(item=>[
    `<b>第 ${esc(item.number)} 题</b>`,
    num(item.full_score,1),
    item.cancelled?'<span class=muted>—</span>':`<b>${esc(item.answer||'')}</b>`,
    item.cancelled?'<span class="badge failed">已取消</span>':'<span class="badge completed">启用</span>',
    item.cancelled?'保留取消':'可更正',
  ]));
}

function ruleEditor(exam,questions){
  if(!canEdit())return notice('观察员为只读账号，不能修订评分规则。');
  const q=questions||[];
  const row=(item)=>`<div class=rule-row data-number="${esc(item.number||'')}"><label>题号<input name=number value="${esc(item.number||'')}" maxlength=20></label><label>满分<input name=full_score type=number min=0.1 step=0.1 value="${item.full_score??10}"></label><label>标准答案<input name=answer maxlength=20 value="${esc(item.answer||'')}"></label><label class=check><input name=cancelled type=checkbox ${item.cancelled?'checked':''}> 取消该题</label></div>`;
  return `<form id=recalc-form><input type=hidden name=exam_id value=${exam?.id||''}><div class=rule-grid id=rule-grid>${q.map(row).join('')}</div><div class=rule-actions><button type=button class=quiet id=add-rule-row>＋ 添加题目</button><button class=primary>按当前规则重评整批</button></div><p class=method>取消题目不计入剩余满分；百分制 = 原始得分 ÷ 剩余满分 × 考试满分。重评前会先保存当前规则，新增题号会一并登记。</p></form>`;
}


function sheetTable(sheets){
  const scores={};
  const batch=batchOf();
  for(const s of batch?.scores||[])scores[s.student_code]=s;
  const showWorking=batch?.status!=='published';
  const rows=(sheets||[]).map(sheet=>{
    const score=scores[sheet.student_code]||{};
    const shown=showWorking?score.percent_score:score.published_percent;
    const isPending=batch?.published_version!=null&&batch.status==='ready';
    return [
      `<b>${esc(sheet.student_code)}</b><small>${esc(sheet.name||'未匹配学员档案')}</small>`,
      sheet.absent?'<span class=muted>缺考</span>':num(shown,2),
      score.conflict?'<span class="badge failed">申诉冲突</span>':(score.published_source&&!showWorking?`<span class="badge completed">${SCORE_SOURCE[score.published_source]||score.published_source}</span>`:isPending?'<span class="badge queued">待发布</span>':'—'),
      score.explanation?`<button class=text-btn data-score-explain="${esc(sheet.student_code)}">分差与贡献</button>`:'—',
    ];
  });
  return table(['学号 / 状态','当前折算分','冲突 / 来源','逐人解释'],rows);
}

function contributionTable(score){
  if(!score)return notice('尚未选择学员。点击任意学员的「分差与贡献」查看逐题解释。');
  if(score.absent)return notice(`${score.student_code} 缺考：保留缺考标记，不参与百分制折算。`);
  const questions=score.contributions?.questions||[];
  const head=`<div class=section-title><h2>${esc(score.student_code)} 的分差解释</h2><span>${score.published_source?`已发布来源：${SCORE_SOURCE[score.published_source]||score.published_source}`:'工作版本'}</span></div>`;
  const summary=`<div class=stats-strip><div><label>最近重评</label><strong>${num(score.percent_score,2)}</strong></div><div><label>上次折算</label><strong>${num(score.previous_percent,2)}</strong></div><div><label>分差</label><strong>${score.delta_percent==null?'—':num(score.delta_percent,2)}</strong></div><div><label>已发布</label><strong>${num(score.published_percent,2)}</strong></div></div>`;
  const rows=questions.map(c=>[
    `第 ${esc(c.number)} 题`,
    num(c.full_score,1),
    esc(c.answer??'—'),
    c.student_answer==null||c.student_answer===''?'<span class=muted>未答</span>':esc(c.student_answer),
    c.cancelled?'<span class=muted>取消</span>':CONTRIBUTION_STATUS[c.status]||esc(c.status),
    num(c.awarded,1),
  ]);
  return `<section class=explain-sheet>${head}${summary}${table(['题号','满分','标准答案','学员作答','判定','得分'],rows)}<pre class=explain>${esc(score.explanation||'')}</pre></section>`;
}

function conflictPanel(batch){
  if(!batch)return '';
  const conflicts=batch.scores.filter(s=>s.conflict);
  const resolved=batch.resolutions||[];
  if(!conflicts.length&&!resolved.length)return '';
  return `<section class=conflict-sheet><div class=section-title><h2>申诉冲突隔离</h2><span>${conflicts.length} 人待裁决</span></div>${conflicts.length?conflicts.map(s=>`<article class=conflict-row><div><b>${esc(s.student_code)}</b> ${esc(s.name||'')}<p>申诉分 ${num(s.appeal_score,2)}（第 ${s.appeal_version} 版）／ 重评分 ${num(s.percent_score,2)}</p></div><form id=resolve-form data-code="${esc(s.student_code)}"><input type=hidden name=expected_appeal_version value="${s.appeal_version}"><select name=decision><option value=keep_appeal>保留申诉分</option><option value=use_recalc>采用重评分</option></select><button class=primary>裁决</button></form></article>`).join(''):notice('当前没有待裁决冲突。')}${resolved.length?`<p class=method>最近裁决：${resolved.slice(0,5).map(r=>`${esc(r.student_code)} · ${RESOLUTION[r.decision]||esc(r.decision)}`).join('；')}</p>`:''}</section>`;
}

function publishKey(batch){
  const state=store.grading;
  if(!state.pendingPublish||state.pendingPublish.version!==batch.version){
    state.pendingPublish={version:batch.version,key:`pub-${batch.id}-v${batch.version}-${(crypto.randomUUID?crypto.randomUUID():Date.now().toString(36))}`};
  }
  return state.pendingPublish.key;
}

function publishPanel(batch){
  if(!batch)return notice('尚无评分批次。完成勘误后点击「重评整批」创建待计算批次。');
  const blocked=conflictCount(batch)>0;
  const canPublish=isAdmin()&&batch.status==='ready'&&!blocked;
  const disabled=!isAdmin()?'（仅管理员可发布）':blocked?'（先逐人裁决冲突）':batch.status!=='ready'?'（先重评形成可发布版本）':'';
  return `<section class=publish-sheet><div class=section-title><h2>发布</h2><span>工作版本 ${batch.version}${batch.published_version!=null?` · 已发布版本 ${batch.published_version}`:''}</span></div><p>整批一次性切换；中断保留旧版。发布后检验只读取完整发布批次。</p><form id=publish-form data-version=${batch.version} data-key="${esc(publishKey(batch))}"><button class=primary ${canPublish?'':'disabled'}>发布本批成绩</button><span class=method>${disabled}</span></form>${batch.published_at?`<p class=method>最近发布：${esc(batch.published_at.slice(0,19).replace('T',' '))} UTC · 校验码 ${esc((batch.published_checksum||'').slice(0,16))}…</p>`:''}</section>`;
}

function verificationPanel(batch){
  const v=store.grading?.verification;
  const canVerify=batch&&batch.published_version!=null&&canEdit();
  const action=canVerify?`<form id=verify-form data-batch=${batch.id}><button class=primary>生成检验结果</button></form>`:'';
  if(!v)return `<section class=verify-sheet>${action}${notice('发布后可生成检验结果。检验能追溯答卷与评分依据，且只读取完整发布批次。')}</section>`;
  const s=v.statistics||{};
  return `<section class=result-sheet><div class=result-heading><div><span class=eyebrow>发布批次检验 · V${v.published_version}</span><h2>有效 ${s.count} 人 · 均分 ${num(s.mean,2)} 分</h2><p>缺考 ${s.absent} 人 · 及格率 ${s.pass_rate==null?'—':num(s.pass_rate*100,1)+'%'} · 中位数 ${num(s.median,2)}</p></div><span class=report-no>检验 № ${String(v.id).padStart(3,'0')}</span></div><div class=stats-strip><div><label>最低</label><strong>${num(s.min,2)}</strong></div><div><label>最高</label><strong>${num(s.max,2)}</strong></div><div><label>及格线</label><strong>${num(s.pass_line,1)}</strong></div><div><label>样本总数</label><strong>${s.total}</strong></div></div><p class=method>评分依据：${(v.rules||[]).map(q=>`第${esc(q.number)}题 ${q.cancelled?'取消':esc(q.answer)+'·'+num(q.full_score,0)}`).join('；')||'—'}</p><p class=method>发布版本 ${v.published_version} · 答卷与评分依据可追溯；列出的答卷与得分均来自该发布批次的冻结快照。</p><p class=checksum>答卷与评分依据校验码<br>${esc(v.checksum)}</p><div class=verify-actions>${action}<button class=quiet data-verify-export=${v.id}>导出检验 JSON ↓</button></div></section>`;
}

function examChooser(){
  const exams=store.grading?.exams||[];
  if(!exams.length)return '';
  const current=store.grading?.exam?.id;
  return `<label class=report-selector>选择考试<select id=grading-exam>${exams.map(e=>`<option value=${e.id} ${e.id===current?'selected':''}>${esc(e.title)} · 满分 ${num(e.max_score,0)} · 答卷 ${e.sheet_count} 份</option>`).join('')}</select></label>`;
}

function newExamForm(){
  if(!canEdit())return '';
  return `<form id=new-exam-form class=new-exam><label>新考试名称<input name=title required minlength=2 maxlength=120 placeholder="2026秋季 · 客观题补测"></label><label>满分<input name=max_score type=number min=1 max=1000 step=1 value=100 required></label><button class=quiet>创建考试</button></form>`;
}

function sheetForm(){
  if(!canEdit())return notice('观察员为只读账号，不能登记答卷或申诉。');
  return `<form id=sheet-form><input type=hidden name=exam_id value="${store.grading?.exam?.id||''}"><label>学号<input name=student_code required maxlength=50 placeholder=ZX-2026-161></label><label>作答（题号:选项，每行一条）<textarea name=answers rows=5 placeholder="1:A&#10;2:B&#10;3:C"></textarea></label><label class=check><input name=absent type=checkbox> 缺考（保留缺考标记）</label><button class=primary>登记答卷</button></form>`;
}

function appealForm(){
  if(!canEdit())return '';
  return `<form id=appeal-form><input type=hidden name=exam_id value="${store.grading?.exam?.id||''}"><label>申诉改分学号<input name=student_code required maxlength=50></label><label>申诉后百分制成绩<input name=score type=number min=0 max=1000 step=0.5 required></label><label>申诉说明<input name=note maxlength=2000 placeholder="复核后加分"></label><button class=quiet>登记申诉改分</button></form>`;
}

export function ruleRowTemplate(){return `<div class=rule-row data-number=""><label>题号<input name=number value="" maxlength=20></label><label>满分<input name=full_score type=number min=0.1 step=0.1 value="10"></label><label>标准答案<input name=answer maxlength=20 value=""></label><label class=check><input name=cancelled type=checkbox> 取消该题</label></div>`;}

export function explainCard(score){return contributionTable(score);}

export function gradingPage(){
  const state=store.grading||{};
  const exam=state.exam;
  const questions=state.questions||[];
  const sheets=state.sheets||[];
  const batch=batchOf();
  if(state.loading)return `<header class=page-head><div><span class=eyebrow>05 / GRADE CORRECTION</span><h1>成绩勘误</h1><p>正在读取考试与答卷…</p></div></header>`;
  if(!exam)return `<header class=page-head><div><span class=eyebrow>05 / GRADE CORRECTION</span><h1>成绩勘误</h1><p>按学号和题号登记客观题答卷，勘误后整批重评、逐人解释、一次性发布。</p></div></header>${newExamForm()}${notice('还没有考试数据。创建考试后登记评分规则与答卷。')}`;
  return `<header class=page-head><div><span class=eyebrow>05 / GRADE CORRECTION</span><h1>成绩勘误</h1><p>按学号与题号登记答卷；勘误可更正答案或取消题目，按剩余满分折算百分制。缺答计零，缺考保留。</p></div>${statusBadge(batch)}</header>${examChooser()}<div class=section-title><h2>评分规则</h2><span>${questions.length} 题 · 满分 ${num(exam.max_score,0)}${batch?` · 工作版本 ${batch.version}`:''}</span></div>${rulesTable(questions)}<div class=split><section><div class=section-title><h2>规则勘误与重评</h2><span>待计算 → 可发布</span></div>${ruleEditor(exam,questions)}<div class=section-title><h2>答卷与成绩</h2><span>${sheets.length} 份答卷</span></div>${sheetTable(sheets)}</section><aside class=side-sheet><span class=eyebrow>答卷登记 / 申诉</span><h2>补录答卷</h2>${sheetForm()}${appealForm()}</aside></div><div id=score-detail>${contributionTable(null)}</div>${conflictPanel(batch)}${publishPanel(batch)}${verificationPanel(batch)}<div class=section-title><h2>说明</h2><span>口径与追溯</span></div>${notice('缺答计零；缺考保留不折算。发布前若成绩被申诉修改，冲突按学员隔离，裁决期间再次改分会被版本号识别并拒绝。整批发布一次性切换，中断保留旧版；检验只读取完整发布批次。')}`;
}
