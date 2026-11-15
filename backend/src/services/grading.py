"""客观题勘误与整批评分.

规则:
- 答卷按学号保存 {题号: 选项}；评分规则按题号保存满分与标准答案。
- 勘误可以更正标准答案，也可以取消题目；取消后按剩余满分折算百分制。
- 缺答计零；缺考保留缺考标记，不参与折算。
- 重评只写工作列 (raw_score / percent_score)，发布时才一次性切换 published_*。
- 申诉按版本号登记；发布时与裁决时都重新核对版本，识别裁决期间再次改分。
"""
import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.schemas.business import QuestionInput
from src.models.entities import (
    AnswerSheet, AppealEvent, Batch, BatchScore, Exam, PublishAttempt, Question,
    Reflection, Resolution, Student, Verification,
)

# 故障注入钩子: 仅在发布事务提交前调用，用于验证「中断保留旧版」。生产环境保持 None。
before_publish_commit = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _qkey(number):
    text = str(number)
    return (0, int(text), '') if text.isdigit() else (1, 0, text)


def _clean(value):
    return None if value is None else str(value).strip()


def rules_payload(rules):
    """稳定的评分规则快照，用于校验码与检验追溯。"""
    return [
        {
            'number': str(q['number']),
            'full_score': float(q['full_score']),
            'answer': _clean(q.get('answer')),
            'cancelled': bool(q.get('cancelled')),
        }
        for q in sorted(rules, key=lambda x: _qkey(x['number']))
    ]


def rules_checksum(rules, max_score) -> str:
    return digest({'max_score': float(max_score), 'rules': rules_payload(rules)})


def _validate_rules(inputs):
    cleaned = []
    seen = set()
    for item in inputs:
        number = str(item.number).strip()
        if number in seen:
            raise HTTPException(400, f'题号 {number} 重复，请合并后再保存')
        seen.add(number)
        cancelled = bool(item.cancelled)
        answer = _clean(item.answer)
        if not cancelled and answer is None:
            raise HTTPException(400, f'第 {number} 题未登记标准答案；请更正答案或取消该题')
        if float(item.full_score) <= 0:
            raise HTTPException(400, f'第 {number} 题满分必须大于 0')
        cleaned.append({
            'number': number,
            'full_score': float(item.full_score),
            'answer': answer,
            'cancelled': cancelled,
        })
    if not cleaned:
        raise HTTPException(400, '至少需要一道题目')
    if all(rule['cancelled'] for rule in cleaned):
        raise HTTPException(409, '全部题目已取消，剩余满分为 0，无法折算百分制；请至少保留一道题')
    return cleaned


def _active_rules(db, exam_id):
    return [
        {'number': q.number, 'full_score': q.full_score,
         'answer': q.answer, 'cancelled': q.cancelled}
        for q in db.scalars(select(Question).where(Question.exam_id == exam_id)
                            .order_by(Question.id)).all()
    ]


def _save_rules(db, exam, rules):
    for question in db.scalars(select(Question).where(Question.exam_id == exam.id)).all():
        db.delete(question)
    db.flush()
    for rule in rules:
        db.add(Question(exam_id=exam.id, number=rule['number'], full_score=rule['full_score'],
                        answer=rule['answer'], cancelled=rule['cancelled']))
    db.flush()


def score_sheet(sheet, rules, max_score):
    """按规则给一份答卷打分.

    cancelled 题目不进入剩余满分；空白作答计零。
    """
    active = [q for q in rules if not q['cancelled']]
    remaining = sum(q['full_score'] for q in active)
    removed = sum(q['full_score'] for q in rules if q['cancelled'])
    if remaining <= 0:
        raise HTTPException(409, '全部题目已取消，剩余满分为 0，无法折算百分制；请恢复至少一道题')
    answers = sheet.answers or {}
    contributions = []
    raw = 0.0
    for q in sorted(rules, key=lambda x: _qkey(x['number'])):
        student_answer = answers.get(q['number'])
        entry = {
            'number': q['number'],
            'full_score': q['full_score'],
            'answer': q['answer'],
            'student_answer': student_answer,
            'cancelled': q['cancelled'],
        }
        if q['cancelled']:
            entry.update(awarded=0.0, status='cancelled')
        else:
            given = _clean(student_answer)
            if given is None or given == '':
                entry.update(awarded=0.0, status='blank')
            elif given == q['answer']:
                entry.update(awarded=q['full_score'], status='correct')
                raw += q['full_score']
            else:
                entry.update(awarded=0.0, status='wrong')
        contributions.append(entry)
    percent = round(raw / remaining * float(max_score), 6)
    return {'raw': round(raw, 6), 'percent': percent, 'contributions': contributions,
            'remaining_full': remaining, 'removed_full': removed}


def _diff_contributions(previous, current):
    old = {str(c['number']): c for c in (previous or [])}
    changes = []
    for item in current:
        number = str(item['number'])
        prior = old.get(number)
        if prior is None:
            changes.append({'number': number, 'from': None, 'to': item['status'],
                            'from_awarded': None, 'to_awarded': item['awarded'],
                            'delta': item['awarded']})
            continue
        if prior.get('status') != item['status'] or prior.get('awarded') != item['awarded']:
            changes.append({
                'number': number,
                'from': prior.get('status'),
                'to': item['status'],
                'from_awarded': prior.get('awarded'),
                'to_awarded': item['awarded'],
                'delta': round(float(item['awarded']) - float(prior.get('awarded') or 0), 6),
            })
    for number, prior in old.items():
        if number not in {str(c['number']) for c in current}:
            changes.append({'number': number, 'from': prior.get('status'), 'to': None,
                            'from_awarded': prior.get('awarded'), 'to_awarded': None,
                            'delta': -float(prior.get('awarded') or 0)})
    return changes


_STATUS_LABEL = {
    'correct': '正确', 'wrong': '错误', 'blank': '空白', 'cancelled': '已取消', 'absent': '缺考',
}


def _explain_score(sheet, rules, current, previous):
    """逐人解释: 分差来自哪些题目、答案更正还是取消。"""
    if sheet.absent:
        return '缺考：保留缺考标记，不参与百分制折算。'

    previous = previous or {}
    prev_raw = previous.get('raw')
    prev_percent = previous.get('percent')
    prev_remaining = previous.get('remaining_full')
    lines = []
    if prev_raw is None and prev_percent is None:
        correct = sum(1 for c in current['contributions'] if c['status'] == 'correct')
        active = sum(1 for c in current['contributions'] if not c['cancelled'])
        lines.append(f'首次重评：{correct}/{active} 题正确，原始得 {current["raw"]:g} 分，'
                     f'按剩余满分 {current["remaining_full"]:g} 折算为 {current["percent"]:.2f} 分。')
    else:
        changes = _diff_contributions(previous.get('contributions'), current['contributions'])
        for change in changes:
            label_from = _STATUS_LABEL.get(change['from'], change['from'])
            label_to = _STATUS_LABEL.get(change['to'], change['to'])
            if change['from'] is None:
                lines.append(f'第 {change["number"]} 题：{label_to}，计 {change["to_awarded"]:g} 分。')
            elif change['to'] is None:
                lines.append(f'第 {change["number"]} 题：从规则中移除。')
            else:
                lines.append(
                    f'第 {change["number"]} 题：{label_from} → {label_to}，'
                    f'得分 {change["from_awarded"]:g} → {change["to_awarded"]:g}（{change["delta"]:+g}）。'
                )
        if prev_remaining is not None and prev_remaining != current['remaining_full']:
            lines.append(f'剩余满分由 {prev_remaining:g} 调整为 {current["remaining_full"]:g}，'
                         f'取消题目合计 {current["removed_full"]:g} 分。')
        if prev_raw is not None:
            lines.append(f'原始分 {prev_raw:g} → {current["raw"]:g}（{current["raw"] - prev_raw:+g}）；'
                         f'折算分 {prev_percent:.2f} → {current["percent"]:.2f}'
                         f'（{current["percent"] - prev_percent:+.2f}）。')
        if not changes and prev_raw == current['raw'] and prev_remaining == current['remaining_full']:
            lines.append('规则与作答未改变，本次重评分数不变。')
    return '\n'.join(lines)


def _previous_state(score):
    """取出最近一次重评结果作为分差基准；首次重评没有基准。"""
    if score is None or score.id is None:
        return None
    data = score.contributions or {}
    if 'questions' not in data:
        return None
    return {
        'raw': score.raw_score,
        'percent': score.percent_score,
        'contributions': data.get('questions') or [],
        'remaining_full': data.get('remaining_full'),
    }


def get_exam(db, exam_id):
    exam = db.get(Exam, exam_id)
    if not exam:
        raise HTTPException(404, '考试不存在')
    return exam


def list_exams(db):
    exams = db.scalars(select(Exam).order_by(Exam.id)).all()
    output = []
    for exam in exams:
        batches = db.scalars(select(Batch).where(Batch.exam_id == exam.id).order_by(Batch.id.desc())).all()
        output.append({
            'id': exam.id, 'title': exam.title, 'max_score': exam.max_score,
            'created_at': exam.created_at,
            'question_count': len(db.scalars(select(Question).where(Question.exam_id == exam.id)).all()),
            'sheet_count': len(db.scalars(select(AnswerSheet).where(AnswerSheet.exam_id == exam.id)).all()),
            'latest_batch': batches[0].id if batches else None,
        })
    return output


def create_exam(db, data):
    if db.scalar(select(Exam).where(Exam.title == data.title)):
        raise HTTPException(409, '考试标题重复，请修改后重试')
    exam = Exam(title=data.title, max_score=float(data.max_score), created_at=now())
    db.add(exam)
    db.commit()
    db.refresh(exam)
    return exam


def upsert_sheet(db, exam_id, data):
    exam = get_exam(db, exam_id)
    sheet = db.scalar(select(AnswerSheet).where(
        AnswerSheet.exam_id == exam.id, AnswerSheet.student_code == data.student_code))
    if sheet is None:
        sheet = AnswerSheet(exam_id=exam.id, student_code=data.student_code, answers={},
                            absent=False, appeal_version=0)
        db.add(sheet)
    answers = {}
    for number, option in (data.answers or {}).items():
        number = str(number).strip()
        if number:
            answers[number] = str(option).strip()
    sheet.answers = answers
    sheet.absent = bool(data.absent)
    if sheet.absent:
        sheet.answers = answers
    db.commit()
    db.refresh(sheet)
    return sheet


def register_appeal(db, exam_id, data, actor):
    exam = get_exam(db, exam_id)
    sheet = db.scalar(select(AnswerSheet).where(
        AnswerSheet.exam_id == exam.id, AnswerSheet.student_code == data.student_code))
    if not sheet:
        raise HTTPException(404, '该学号尚未登记答卷')
    if sheet.absent and data.score is not None:
        raise HTTPException(409, '该学员缺考，成绩保留为空，不能登记申诉改分')
    if data.expected_version is not None and data.expected_version != sheet.appeal_version:
        raise HTTPException(409, '申诉成绩已被他人修改，请刷新后重试')
    sheet.appeal_version = (sheet.appeal_version or 0) + 1
    sheet.appeal_score = data.score
    sheet.appeal_note = data.note
    db.add(AppealEvent(exam_id=exam.id, student_code=sheet.student_code,
                       version=sheet.appeal_version, score=data.score, note=data.note,
                       actor=actor, created_at=now()))
    # 尚未发布的批次立即隔离冲突；最终发布时还会重新核对一次。
    working = db.scalars(select(Batch).where(Batch.exam_id == exam.id,
                                             Batch.status != 'published')).all()
    for batch in working:
        score = db.scalar(select(BatchScore).where(
            BatchScore.batch_id == batch.id, BatchScore.student_code == sheet.student_code))
        if score:
            score.conflict = data.score is not None and sheet.appeal_version > (score.resolved_appeal_version or 0)
    db.commit()
    db.refresh(sheet)
    return sheet


def get_batch(db, batch_id):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, '评分批次不存在')
    return batch


def get_or_create_batch(db, exam_id):
    exam = get_exam(db, exam_id)
    batch = db.scalar(select(Batch).where(Batch.exam_id == exam.id).order_by(Batch.id.desc()))
    if batch:
        return batch
    batch = Batch(exam_id=exam.id, status='pending', version=0, created_at=now())
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def save_rules(db, exam_id, rules_input):
    """独立保存评分规则，便于先登记规则再补答卷。"""
    exam = get_exam(db, exam_id)
    rules = _validate_rules(rules_input)
    _save_rules(db, exam, rules)
    db.commit()
    return [serialize_question(q) for q in db.scalars(
        select(Question).where(Question.exam_id == exam.id).order_by(Question.id)).all()]


def recalc(db, batch_id, rules_input, actor):
    """按最新规则重评整批，逐人记录分差与题目贡献。

    rules_input 为空时使用已保存的评分规则。
    """
    batch = db.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
    if not batch:
        raise HTTPException(404, '评分批次不存在')
    exam = get_exam(db, batch.exam_id)
    rules = _validate_rules(rules_input) if rules_input else _validate_rules(
        [QuestionInput(**q) for q in _active_rules(db, exam.id)])
    _save_rules(db, exam, rules)
    payload = rules_payload(rules)
    checksum = rules_checksum(payload, exam.max_score)

    sheets = db.scalars(select(AnswerSheet).where(AnswerSheet.exam_id == exam.id)
                        .order_by(AnswerSheet.student_code)).all()
    if not sheets:
        raise HTTPException(409, '该考试还没有登记任何答卷，无法重评')

    existing = {s.student_code: s for s in db.scalars(
        select(BatchScore).where(BatchScore.batch_id == batch.id)).all()}
    scored = []
    conflicts = 0
    appellated = 0
    absent_count = 0
    for sheet in sheets:
        score = existing.get(sheet.student_code)
        previous = _previous_state(score)
        if score is None:
            score = BatchScore(batch_id=batch.id, student_code=sheet.student_code)
            db.add(score)
        score.previous_raw = previous.get('raw') if previous else None
        score.previous_percent = previous.get('percent') if previous else None
        if sheet.absent:
            absent_count += 1
            score.raw_score = None
            score.percent_score = None
            score.absent = True
            score.contributions = {'state': 'absent'}
            score.explanation = '缺考：保留缺考标记，不参与百分制折算。'
        else:
            result = score_sheet(sheet, payload, exam.max_score)
            contributions = {
                'questions': result['contributions'],
                'previous_raw': previous.get('raw') if previous else None,
                'previous_percent': previous.get('percent') if previous else None,
                'delta_raw': None if not previous or previous.get('raw') is None
                else round(result['raw'] - previous['raw'], 6),
                'delta_percent': None if not previous or previous.get('percent') is None
                else round(result['percent'] - previous['percent'], 6),
                'remaining_full': result['remaining_full'],
                'previous_remaining_full': previous.get('remaining_full') if previous else None,
                'removed_full': result['removed_full'],
                'changes': _diff_contributions(previous.get('contributions') if previous else None,
                                               result['contributions']),
                'rule_checksum': checksum,
            }
            score.raw_score = result['raw']
            score.percent_score = result['percent']
            score.absent = False
            score.contributions = contributions
            score.explanation = _explain_score(sheet, payload, {
                'raw': result['raw'], 'percent': result['percent'],
                'contributions': result['contributions'],
                'remaining_full': result['remaining_full'],
                'removed_full': result['removed_full'],
            }, previous)
        if sheet.appeal_score is not None:
            appellated += 1
        score.conflict = bool(sheet.appeal_score is not None
                              and (sheet.appeal_version or 0) > (score.resolved_appeal_version or 0))
        if score.conflict:
            conflicts += 1
        db.flush()
        scored.append((sheet, result if not sheet.absent else None))

    batch.version = (batch.version or 0) + 1
    batch.status = 'ready'
    batch.explanation = (f'规则校验码 {checksum[:16]}…；取消题目合计 '
                         f'{sum(q["full_score"] for q in payload if q["cancelled"]):g} 分，'
                         f'剩余满分 {sum(q["full_score"] for q in payload if not q["cancelled"]):g} 分。')
    batch.recalc_checksum = checksum
    reflection = Reflection(
        batch_id=batch.id, version=batch.version, created_at=now(), rule_version=checksum,
        removed_full=sum(q['full_score'] for q in payload if q['cancelled']),
        remaining_full=sum(q['full_score'] for q in payload if not q['cancelled']),
        appellated=appellated, conflicts=conflicts,
        summary={'rules': payload, 'max_score': exam.max_score, 'checksum': checksum,
                 'total': len(scored), 'absent': absent_count, 'appellated': appellated,
                 'conflicts': conflicts, 'actor': actor},
    )
    db.add(reflection)
    db.commit()
    return batch


def _live_conflict(score, sheet):
    return bool(sheet and sheet.appeal_score is not None
                and (sheet.appeal_version or 0) > (score.resolved_appeal_version or 0))


def resolve_conflict(db, batch_id, data, actor):
    batch = db.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
    if not batch:
        raise HTTPException(404, '评分批次不存在')
    if batch.status == 'published':
        raise HTTPException(409, '该批次已发布，不能再裁决；请先重评形成新的工作版本')
    score = db.scalar(select(BatchScore).where(
        BatchScore.batch_id == batch.id, BatchScore.student_code == data.student_code))
    if not score:
        raise HTTPException(404, '该学员不在本批次中')
    sheet = db.scalar(select(AnswerSheet).where(
        AnswerSheet.exam_id == batch.exam_id, AnswerSheet.student_code == data.student_code))
    if not sheet or sheet.appeal_score is None:
        raise HTTPException(409, '该学员当前没有申诉改分')
    if data.expected_appeal_version != (sheet.appeal_version or 0):
        raise HTTPException(409, '申诉成绩在裁决期间被再次修改，请刷新并重新裁决')
    db.add(Resolution(batch_id=batch.id, student_code=score.student_code,
                      decision=data.decision, appeal_version=sheet.appeal_version,
                      appeal_score=sheet.appeal_score, raw_score=score.raw_score,
                      actor=actor, created_at=now()))
    score.resolved_appeal_version = sheet.appeal_version
    score.resolution_decision = data.decision
    score.conflict = False
    if data.decision == 'keep_appeal':
        score.explanation = (score.explanation or '') + \
            f'\n裁决：保留申诉分 {sheet.appeal_score:g}（第 {sheet.appeal_version} 版申诉）。'
    else:
        score.explanation = (score.explanation or '') + \
            f'\n裁决：采用重评分 {score.percent_score:.2f}，覆盖第 {sheet.appeal_version} 版申诉。'
    db.commit()
    return score


def publish(db, batch_id, request_key, expected_version, actor):
    """整批一次性发布.

    - 同一 request_key 重试直接返回已发布结果，不重复改分。
    - expected_version 过期说明工作版本已被重评，拒绝覆盖。
    - 所有 published_* 在同一事务写入；中断回滚保留旧版。
    """
    batch = db.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
    if not batch:
        raise HTTPException(404, '评分批次不存在')
    attempt = db.scalar(select(PublishAttempt).where(
        PublishAttempt.batch_id == batch.id, PublishAttempt.request_key == request_key))
    if attempt and attempt.status == 'done':
        # 该请求已完成：只有当它对应的版本仍是当前已发布版本时才幂等返回；
        # 若之后又发布了更新版本，旧请求不能再声称成功。
        if attempt.version == batch.published_version:
            return batch, True
        raise HTTPException(409, f'该发布请求对应版本 {attempt.version} 已被后续版本 '
                            f'{batch.published_version} 取代，请刷新后重新发布')
    if batch.status not in ('ready', 'published'):
        raise HTTPException(409, '尚未重评，当前批次不可发布')
    if expected_version != batch.version:
        raise HTTPException(409, f'成绩已被重新计算（当前工作版本 {batch.version}），请刷新后重新发布')
    if batch.published_version == batch.version:
        # 该版本已经完整发布；同一或并发请求直接返回既有结果，不重复改分。
        return batch, True
    if batch.status != 'ready':
        raise HTTPException(409, '当前批次不在可发布状态')

    if attempt is None:
        attempt = PublishAttempt(batch_id=batch.id, request_key=request_key,
                                 status='pending', created_at=now())
        db.add(attempt)
        try:
            db.flush()
        except IntegrityError:
            # 同一幂等键的请求已由另一并发事务完成；重新读取其结果，而不是重复改分。
            db.rollback()
            batch = get_batch(db, batch_id)
            existing_attempt = db.scalar(select(PublishAttempt).where(
                PublishAttempt.batch_id == batch_id,
                PublishAttempt.request_key == request_key))
            if existing_attempt and existing_attempt.status == 'done' \
                    and existing_attempt.version == batch.published_version:
                return batch, True
            raise HTTPException(409, '同一发布请求正在处理中，请稍后重试')

    scores = db.scalars(select(BatchScore).where(BatchScore.batch_id == batch.id)
                        .order_by(BatchScore.student_code)).all()
    sheets = {s.student_code: s for s in db.scalars(
        select(AnswerSheet).where(AnswerSheet.exam_id == batch.exam_id)).all()}
    codes = [s.student_code for s in scores]
    students = {s.code: s for s in db.scalars(
        select(Student).where(Student.code.in_(codes))).all()} if codes else {}
    conflicts = []
    for score in scores:
        sheet = sheets.get(score.student_code)
        # 发布前重新核对申诉版本，识别重评后、裁决期间插入的改分。
        if _live_conflict(score, sheet):
            score.conflict = True
            conflicts.append(score.student_code)
        elif not score.conflict:
            score.conflict = False
    if conflicts:
        # 冲突时只持久化隔离标记，不留下 pending 幂等键，裁决后可用同一 key 重试。
        if attempt is not None and attempt.status != 'done':
            db.delete(attempt)
        db.commit()
        raise HTTPException(409, '以下学员的成绩在发布前被申诉修改，存在未裁决冲突：'
                            + '、'.join(sorted(conflicts)) + '；请逐人裁决后重新发布')

    reflection = db.scalar(select(Reflection).where(
        Reflection.batch_id == batch.id, Reflection.version == batch.version))
    if not reflection:
        raise HTTPException(409, '缺少工作版本的评分规则快照，请重新计算后再发布')

    snapshot_scores = []
    for score in scores:
        sheet = sheets.get(score.student_code)
        student = students.get(score.student_code)
        score.conflict = False
        if score.absent:
            score.published_score = None
            score.published_percent = None
            score.published_source = 'absent'
        else:
            use_appeal = (sheet is not None and sheet.appeal_score is not None
                          and score.resolution_decision == 'keep_appeal'
                          and (sheet.appeal_version or 0) == (score.resolved_appeal_version or 0))
            if use_appeal:
                score.published_score = score.raw_score
                score.published_percent = float(sheet.appeal_score)
                score.published_source = 'appeal'
            else:
                score.published_score = score.raw_score
                score.published_percent = score.percent_score
                score.published_source = 'recalculated'
        snapshot_scores.append({
            'student_code': score.student_code,
            'name': student.name if student else None,
            'campus': student.campus if student else None,
            'absent': bool(score.absent),
            'answers': dict(sheet.answers or {}) if sheet else {},
            'published_score': score.published_score,
            'published_percent': score.published_percent,
            'published_source': score.published_source,
            'resolution_decision': score.resolution_decision or '',
            'resolved_appeal_version': score.resolved_appeal_version or 0,
            'appeal_version': sheet.appeal_version if sheet else 0,
            'appeal_score': sheet.appeal_score if sheet else None,
            'contributions': score.contributions or {},
        })

    entries = [[s['student_code'], s['absent'], s['published_score'], s['published_percent'],
                s['published_source']] for s in snapshot_scores]
    checksum = digest({'batch_id': batch.id, 'version': batch.version, 'entries': entries})
    published_at = now()
    batch.published_snapshot = {
        'entries': entries,
        'version': batch.version,
        'captured_at': published_at,
        'scores': snapshot_scores,
    }
    batch.published_version = batch.version
    batch.published_checksum = checksum
    batch.published_at = published_at
    batch.status = 'published'
    reflection.published_snapshot = batch.published_snapshot
    reflection.published_at = published_at
    attempt.status = 'done'
    attempt.version = batch.version
    attempt.finished_at = now()

    if before_publish_commit is not None:
        try:
            before_publish_commit(db)
        except Exception:
            db.rollback()
            raise
    db.commit()
    db.refresh(batch)
    return batch, False


def _snapshot_entries(snapshot):
    return [[s['student_code'], bool(s['absent']), s['published_score'],
             s['published_percent'], s['published_source']]
            for s in (snapshot or {}).get('scores') or []]


def verify(db, batch_id, actor):
    """检验只读取发布时冻结的完整批次快照.

    冻结快照保存在该版本的 Reflection 上，发布后不再修改；batch.published_snapshot
    只是同版本的指针。两者中任何一份被篡改或版本错位，都拒绝检验。
    """
    batch = get_batch(db, batch_id)
    if batch.published_version is None:
        raise HTTPException(409, '批次尚未完整发布，不能进行检验；请等待整批切换完成')
    reflection = db.scalar(select(Reflection).where(
        Reflection.batch_id == batch.id, Reflection.version == batch.published_version))
    if not reflection or not reflection.published_snapshot:
        raise HTTPException(409, '缺少该发布版本的评分规则快照，不能检验')
    frozen = reflection.published_snapshot
    scores = frozen.get('scores') or []
    if not scores:
        raise HTTPException(409, '发布批次没有完整快照，视为不完整，不能检验')
    if frozen.get('version') != batch.published_version:
        raise HTTPException(409, '发布批次快照版本不一致，不能检验')
    frozen_entries = _snapshot_entries(frozen)
    checksum = digest({'batch_id': batch.id, 'version': frozen.get('version'),
                       'entries': frozen_entries})
    if checksum != batch.published_checksum:
        raise HTTPException(409, '发布批次校验不通过，可能缺少或混入未发布日期成绩，不能检验')
    pointer = batch.published_snapshot or {}
    if pointer.get('version') != batch.published_version or _snapshot_entries(pointer) != frozen_entries:
        raise HTTPException(409, '发布指针与冻结快照不一致，批次可能未完整切换，不能检验')
    if any((not s['absent']) and (s['published_percent'] is None or not s['published_source'])
           for s in scores):
        raise HTTPException(409, '发布批次存在未完成折算的成绩，不能检验')
    exam = get_exam(db, batch.exam_id)

    values = [s['published_percent'] for s in scores
              if not s['absent'] and s['published_percent'] is not None]
    ordered = sorted(values)
    count = len(ordered)

    def percentile(p):
        if not ordered:
            return None
        position = (len(ordered) - 1) * p
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    pass_line = float(exam.max_score) * 0.6
    buckets = {'<60': 0, '60-69': 0, '70-79': 0, '80-89': 0, '90-100': 0}
    for value in values:
        if value < 60:
            buckets['<60'] += 1
        elif value < 70:
            buckets['60-69'] += 1
        elif value < 80:
            buckets['70-79'] += 1
        elif value < 90:
            buckets['80-89'] += 1
        else:
            buckets['90-100'] += 1
    statistics = {
        'count': count,
        'absent': sum(1 for s in scores if s['absent']),
        'total': len(scores),
        'mean': round(sum(values) / count, 4) if count else None,
        'median': round(percentile(0.5), 4) if count else None,
        'min': ordered[0] if ordered else None,
        'max': ordered[-1] if ordered else None,
        'pass_line': pass_line,
        'pass_rate': round(sum(1 for v in values if v >= pass_line) / count, 4) if count else None,
        'buckets': buckets,
    }

    evidence = {
        'exam': {'id': exam.id, 'title': exam.title, 'max_score': exam.max_score},
        'published_version': batch.published_version,
        'published_at': batch.published_at,
        'published_checksum': batch.published_checksum,
        'rule_version': reflection.rule_version,
        'rules': reflection.summary.get('rules', []),
        'reflection': {
            'id': reflection.id, 'version': reflection.version,
            'removed_full': reflection.removed_full,
            'remaining_full': reflection.remaining_full,
            'appellated': reflection.appellated, 'conflicts': reflection.conflicts,
            'summary': reflection.summary,
        },
        'scores': [
            {
                'student_code': s['student_code'],
                'name': s.get('name'),
                'campus': s.get('campus'),
                'absent': s['absent'],
                'answers': s.get('answers') or {},
                'raw_score': s['published_score'],
                'percent_score': s['published_percent'],
                'source': s['published_source'],
                'resolution_decision': s.get('resolution_decision') or '',
                'appeal_version': s.get('appeal_version') or 0,
                'appeal_score': s.get('appeal_score'),
                'contributions': s.get('contributions') or {},
            }
            for s in scores
        ],
    }
    item = Verification(batch_id=batch.id, exam_id=exam.id,
                        published_version=batch.published_version,
                        checksum=batch.published_checksum, reflection_id=reflection.id,
                        rules=reflection.summary.get('rules', []), statistics=statistics,
                        evidence=evidence, created_by=actor, created_at=now())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def serialize_verification(item):
    return {
        'id': item.id, 'batch_id': item.batch_id, 'exam_id': item.exam_id,
        'published_version': item.published_version, 'checksum': item.checksum,
        'reflection_id': item.reflection_id, 'rules': item.rules,
        'statistics': item.statistics, 'evidence': item.evidence,
        'created_by': item.created_by, 'created_at': item.created_at,
    }


def serialize_batch(db, batch):
    scores = db.scalars(select(BatchScore).where(BatchScore.batch_id == batch.id)
                        .order_by(BatchScore.student_code)).all()
    sheets = {s.student_code: s for s in db.scalars(
        select(AnswerSheet).where(AnswerSheet.exam_id == batch.exam_id)).all()}
    codes = [s.student_code for s in scores]
    students = {s.code: s for s in db.scalars(
        select(Student).where(Student.code.in_(codes))).all()} if codes else {}
    reflections = db.scalars(select(Reflection).where(Reflection.batch_id == batch.id)
                             .order_by(Reflection.version.desc())).all()
    resolutions = db.scalars(select(Resolution).where(Resolution.batch_id == batch.id)
                             .order_by(Resolution.id.desc()).limit(50)).all()
    latest = reflections[0] if reflections else None
    score_rows = []
    for score in scores:
        sheet = sheets.get(score.student_code)
        student = students.get(score.student_code)
        score_rows.append({
            'student_code': score.student_code,
            'name': student.name if student else None,
            'campus': student.campus if student else None,
            'absent': score.absent,
            'raw_score': score.raw_score,
            'percent_score': score.percent_score,
            'previous_raw': score.previous_raw,
            'previous_percent': score.previous_percent,
            'delta_raw': (score.contributions or {}).get('delta_raw'),
            'delta_percent': (score.contributions or {}).get('delta_percent'),
            'contributions': score.contributions or {},
            'explanation': score.explanation,
            'conflict': score.conflict,
            'appeal_version': sheet.appeal_version if sheet else 0,
            'appeal_score': sheet.appeal_score if sheet else None,
            'appeal_note': sheet.appeal_note if sheet else '',
            'resolved_appeal_version': score.resolved_appeal_version,
            'resolution_decision': score.resolution_decision,
            'published_score': score.published_score,
            'published_percent': score.published_percent,
            'published_source': score.published_source,
        })
    return {
        'id': batch.id,
        'exam_id': batch.exam_id,
        'status': batch.status,
        'version': batch.version,
        'published_version': batch.published_version,
        'published_at': batch.published_at,
        'published_checksum': batch.published_checksum,
        'recalc_checksum': batch.recalc_checksum,
        'explanation': batch.explanation,
        'created_at': batch.created_at,
        'conflicts': [row['student_code'] for row in score_rows if row['conflict']],
        'scores': score_rows,
        'reflection': None if not latest else {
            'id': latest.id, 'version': latest.version, 'created_at': latest.created_at,
            'rule_version': latest.rule_version, 'removed_full': latest.removed_full,
            'remaining_full': latest.remaining_full, 'appellated': latest.appellated,
            'conflicts': latest.conflicts, 'summary': latest.summary,
        },
        'resolutions': [
            {'student_code': r.student_code, 'decision': r.decision,
             'appeal_version': r.appeal_version, 'appeal_score': r.appeal_score,
             'raw_score': r.raw_score, 'actor': r.actor, 'created_at': r.created_at}
            for r in resolutions
        ],
    }


def serialize_question(question):
    return {'id': question.id, 'number': question.number, 'full_score': question.full_score,
            'answer': question.answer, 'cancelled': question.cancelled}


def serialize_sheet(sheet, student=None):
    return {
        'student_code': sheet.student_code,
        'answers': sheet.answers or {},
        'absent': sheet.absent,
        'appeal_score': sheet.appeal_score,
        'appeal_note': sheet.appeal_note,
        'appeal_version': sheet.appeal_version,
        'name': student.name if student else None,
        'campus': student.campus if student else None,
    }


def workspace(db, exam_id):
    exam = get_exam(db, exam_id)
    questions = db.scalars(select(Question).where(Question.exam_id == exam.id)
                           .order_by(Question.id)).all()
    sheets = db.scalars(select(AnswerSheet).where(AnswerSheet.exam_id == exam.id)
                        .order_by(AnswerSheet.student_code)).all()
    codes = [s.student_code for s in sheets]
    students = {s.code: s for s in db.scalars(
        select(Student).where(Student.code.in_(codes))).all()} if codes else {}
    batches = db.scalars(select(Batch).where(Batch.exam_id == exam.id).order_by(Batch.id.desc())).all()
    active = next((b for b in batches if b.status != 'published'), None)
    if active is None:
        active = batches[0] if batches else None
    verification = db.scalar(select(Verification).where(Verification.exam_id == exam.id)
                             .order_by(Verification.id.desc()))
    return {
        'exam': {'id': exam.id, 'title': exam.title, 'max_score': exam.max_score,
                 'created_at': exam.created_at},
        'questions': [serialize_question(q) for q in questions],
        'sheets': [serialize_sheet(s, students.get(s.student_code)) for s in sheets],
        'batch': serialize_batch(db, active) if active else None,
        'batches': [{'id': b.id, 'status': b.status, 'version': b.version,
                     'published_version': b.published_version,
                     'published_at': b.published_at} for b in batches],
        'latest_verification': serialize_verification(verification) if verification else None,
    }
