from sqlalchemy import String,Float,Boolean,Integer,ForeignKey,JSON,Text,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from src.config.database import Base
class User(Base):
    __tablename__='users'
    id:Mapped[int]=mapped_column(primary_key=True)
    email:Mapped[str]=mapped_column(String(120),unique=True)
    role:Mapped[str]=mapped_column(String(20))
    password_hash:Mapped[str]=mapped_column(String(200))
class Student(Base):
    __tablename__='students'
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str]=mapped_column(String(50),unique=True)
    name:Mapped[str]=mapped_column(String(80))
    campus:Mapped[str]=mapped_column(String(50))
    group:Mapped[str]=mapped_column(String(20))
    score:Mapped[float|None]=mapped_column(Float,nullable=True)
    absent:Mapped[bool]=mapped_column(Boolean,default=False)
class Plan(Base):
    __tablename__='plans'
    id:Mapped[int]=mapped_column(primary_key=True)
    title:Mapped[str]=mapped_column(String(120),unique=True)
    campus:Mapped[str]=mapped_column(String(50),default='全部')
    exclude_absent:Mapped[bool]=mapped_column(Boolean,default=True)
    description:Mapped[str]=mapped_column(Text,default='')
class Report(Base):
    __tablename__='reports'
    id:Mapped[int]=mapped_column(primary_key=True)
    plan_id:Mapped[int]=mapped_column(ForeignKey('plans.id'))
    status:Mapped[str]=mapped_column(String(20),default='queued')
    result:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    error:Mapped[str]=mapped_column(Text,default='')
    conclusion:Mapped[str]=mapped_column(Text,default='')
    published:Mapped[bool]=mapped_column(Boolean,default=False)
    created_at:Mapped[str]=mapped_column(String(40))
class Audit(Base):
    __tablename__='audit_logs'
    id:Mapped[int]=mapped_column(primary_key=True)
    actor:Mapped[str]=mapped_column(String(120))
    action:Mapped[str]=mapped_column(String(200))
    status:Mapped[int]=mapped_column(Integer)
    created_at:Mapped[str]=mapped_column(String(40))

class Exam(Base):
    """一次考试的客观题答卷与评分规则容器。"""
    __tablename__='exams'
    id:Mapped[int]=mapped_column(primary_key=True)
    title:Mapped[str]=mapped_column(String(120),unique=True)
    max_score:Mapped[float]=mapped_column(Float,default=100.0)
    created_at:Mapped[str]=mapped_column(String(40))

class Question(Base):
    """题号、满分与标准答案。answer=None 表示取消该题。"""
    __tablename__='questions'
    __table_args__=(UniqueConstraint('exam_id','number',name='uq_question_exam_number'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    exam_id:Mapped[int]=mapped_column(ForeignKey('exams.id'))
    number:Mapped[str]=mapped_column(String(20))
    full_score:Mapped[float]=mapped_column(Float)
    answer:Mapped[str|None]=mapped_column(String(20),nullable=True)
    cancelled:Mapped[bool]=mapped_column(Boolean,default=False)

class AnswerSheet(Base):
    """按学号登记的客观题答卷。answers 为 {题号: 选项}；absent 表示缺考。"""
    __tablename__='answer_sheets'
    __table_args__=(UniqueConstraint('exam_id','student_code',name='uq_sheet_exam_code'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    exam_id:Mapped[int]=mapped_column(ForeignKey('exams.id'))
    student_code:Mapped[str]=mapped_column(String(50))
    answers:Mapped[dict]=mapped_column(JSON,default=dict)
    absent:Mapped[bool]=mapped_column(Boolean,default=False)
    appeal_score:Mapped[float|None]=mapped_column(Float,nullable=True)
    appeal_note:Mapped[str]=mapped_column(Text,default='')
    appeal_version:Mapped[int]=mapped_column(Integer,default=0)

class Batch(Base):
    """一次整批评分。pending→ready→published；published 后旧版本保持不变。"""
    __tablename__='batches'
    id:Mapped[int]=mapped_column(primary_key=True)
    exam_id:Mapped[int]=mapped_column(ForeignKey('exams.id'))
    status:Mapped[str]=mapped_column(String(20),default='pending')
    version:Mapped[int]=mapped_column(Integer,default=1)
    published_version:Mapped[int|None]=mapped_column(Integer,nullable=True)
    recalc_checksum:Mapped[str]=mapped_column(String(80),default='')
    published_checksum:Mapped[str]=mapped_column(String(80),default='')
    published_snapshot:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    created_at:Mapped[str]=mapped_column(String(40))
    published_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    explanation:Mapped[str]=mapped_column(Text,default='')

class BatchScore(Base):
    """批次内逐人成绩。raw 保留最近一次重评结果，published 只在整批发布时写入。"""
    __tablename__='batch_scores'
    __table_args__=(UniqueConstraint('batch_id','student_code',name='uq_score_batch_code'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('batches.id'))
    student_code:Mapped[str]=mapped_column(String(50))
    raw_score:Mapped[float|None]=mapped_column(Float,nullable=True)
    percent_score:Mapped[float|None]=mapped_column(Float,nullable=True)
    previous_raw:Mapped[float|None]=mapped_column(Float,nullable=True)
    previous_percent:Mapped[float|None]=mapped_column(Float,nullable=True)
    absent:Mapped[bool]=mapped_column(Boolean,default=False)
    contributions:Mapped[dict]=mapped_column(JSON,default=dict)
    conflict:Mapped[bool]=mapped_column(Boolean,default=False)
    resolved_appeal_version:Mapped[int]=mapped_column(Integer,default=0)
    resolution_decision:Mapped[str]=mapped_column(String(20),default='')
    published_score:Mapped[float|None]=mapped_column(Float,nullable=True)
    published_percent:Mapped[float|None]=mapped_column(Float,nullable=True)
    published_source:Mapped[str]=mapped_column(String(20),default='')
    explanation:Mapped[str]=mapped_column(Text,default='')

class Reflection(Base):
    """重评快照：每次计算将题目版本、分差及贡献固化为可追溯的解释。

    published_snapshot 在发布该版本时写入，保留该版本的整批已发布成绩，
    后续再重评/再发布都不会修改它。
    """
    __tablename__='reflections'
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('batches.id'))
    version:Mapped[int]=mapped_column(Integer,default=1)
    created_at:Mapped[str]=mapped_column(String(40))
    rule_version:Mapped[str]=mapped_column(String(80),default='')
    removed_full:Mapped[float]=mapped_column(Float,default=0.0)
    remaining_full:Mapped[float]=mapped_column(Float,default=0.0)
    appellated:Mapped[int]=mapped_column(Integer,default=0)
    conflicts:Mapped[int]=mapped_column(Integer,default=0)
    summary:Mapped[dict]=mapped_column(JSON,default=dict)
    published_snapshot:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    published_at:Mapped[str|None]=mapped_column(String(40),nullable=True)

class AppealEvent(Base):
    """申诉改分流水：每次修改都带版本号，用于检测裁决期间的并发改分。"""
    __tablename__='appeal_events'
    id:Mapped[int]=mapped_column(primary_key=True)
    exam_id:Mapped[int]=mapped_column(ForeignKey('exams.id'))
    student_code:Mapped[str]=mapped_column(String(50))
    version:Mapped[int]=mapped_column(Integer)
    score:Mapped[float|None]=mapped_column(Float,nullable=True)
    note:Mapped[str]=mapped_column(Text,default='')
    actor:Mapped[str]=mapped_column(String(120))
    created_at:Mapped[str]=mapped_column(String(40))

class Resolution(Base):
    """冲突裁决记录。decision: keep_appeal / use_recalc。"""
    __tablename__='resolutions'
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('batches.id'))
    student_code:Mapped[str]=mapped_column(String(50))
    decision:Mapped[str]=mapped_column(String(20))
    appeal_version:Mapped[int]=mapped_column(Integer,default=0)
    appeal_score:Mapped[float|None]=mapped_column(Float,nullable=True)
    raw_score:Mapped[float|None]=mapped_column(Float,nullable=True)
    actor:Mapped[str]=mapped_column(String(120))
    created_at:Mapped[str]=mapped_column(String(40))

class PublishAttempt(Base):
    """发布幂等键：同一次发布重试不会重复改分。"""
    __tablename__='publish_attempts'
    __table_args__=(UniqueConstraint('batch_id','request_key',name='uq_publish_batch_key'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('batches.id'))
    request_key:Mapped[str]=mapped_column(String(80))
    status:Mapped[str]=mapped_column(String(20),default='pending')
    version:Mapped[int|None]=mapped_column(Integer,nullable=True)
    created_at:Mapped[str]=mapped_column(String(40))
    finished_at:Mapped[str|None]=mapped_column(String(40),nullable=True)

class Verification(Base):
    """成绩检验：只读取完整发布批次，并保留评分规则与答卷依据。"""
    __tablename__='verifications'
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('batches.id'))
    exam_id:Mapped[int]=mapped_column(ForeignKey('exams.id'))
    published_version:Mapped[int]=mapped_column(Integer)
    checksum:Mapped[str]=mapped_column(String(80))
    reflection_id:Mapped[int|None]=mapped_column(Integer,nullable=True)
    rules:Mapped[list]=mapped_column(JSON,default=list)
    statistics:Mapped[dict]=mapped_column(JSON,default=dict)
    evidence:Mapped[dict]=mapped_column(JSON,default=dict)
    created_by:Mapped[str]=mapped_column(String(120),default='')
    created_at:Mapped[str]=mapped_column(String(40))


