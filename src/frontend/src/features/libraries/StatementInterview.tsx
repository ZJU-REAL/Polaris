import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import {
  api,
  type StatementInterviewAnswer,
  type StatementInterviewQuestion,
} from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   方向描述访谈：AI 按「研究问题 / 研究对象 / 子问题 / 方法类型」四个环节提问，
   每题给几个可勾选的候选，末尾生成一段英文描述。

   为什么要有这个：statement 同时决定粗排挑哪些论文和 LLM 怎么打分，写含糊了后果很实在
   ——线上 12 个库的 statement 全是 3~31 字符的标签（有一个只有三个字母），向量排在
   最前的是与方向毫不相干的论文。光给写作提示挡不住，得改成引导式产出。
   ============================================================ */

export function StatementInterview({
  open,
  topic,
  onClose,
  onDone,
}: {
  open: boolean;
  /** 研究方向名称，作为访谈的出发点 */
  topic: string;
  onClose: () => void;
  /** 用户确认后回填的描述 */
  onDone: (statement: string) => void;
}) {
  const [answers, setAnswers] = useState<StatementInterviewAnswer[]>([]);
  const [question, setQuestion] = useState<StatementInterviewQuestion | null>(null);
  const [step, setStep] = useState(1);
  const [total, setTotal] = useState(4);
  const [picked, setPicked] = useState<string[]>([]);
  const [custom, setCustom] = useState('');
  const [statement, setStatement] = useState('');
  const [busy, setBusy] = useState(false);

  const advance = useCallback(
    async (next: StatementInterviewAnswer[]) => {
      setBusy(true);
      try {
        const res = await api.statementInterview(topic, next);
        setStep(res.step);
        setTotal(res.total);
        if (res.done) {
          setQuestion(null);
          setStatement(res.statement ?? '');
        } else {
          setQuestion(res.question ?? null);
          setPicked([]);
          setCustom('');
        }
      } catch (e) {
        toast(
          `${tr('访谈出错：', 'Interview failed: ')}${e instanceof Error ? e.message : String(e)}`,
          'error',
        );
      } finally {
        setBusy(false);
      }
    },
    [topic],
  );

  // 打开时从头开始；关掉后再打开是一次新访谈（题目可能已经改了）
  useEffect(() => {
    if (!open) return;
    setAnswers([]);
    setQuestion(null);
    setStatement('');
    setPicked([]);
    setCustom('');
    void advance([]);
  }, [open, advance]);

  const submitAnswer = () => {
    if (!question) return;
    const next = [
      ...answers,
      { stage: question.stage, selected: picked, custom: custom.trim() },
    ];
    setAnswers(next);
    void advance(next);
  };

  const toggle = (opt: string) =>
    setPicked((prev) => (prev.includes(opt) ? prev.filter((x) => x !== opt) : [...prev, opt]));

  const nothingChosen = picked.length === 0 && !custom.trim();

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('AI 访谈：把方向说清楚', 'AI interview: pin down the direction')}
      sub={tr(
        '这段描述既用来自动挑论文，也用来给论文打分——写得越具体，收得越准。',
        'This text both selects and scores papers, so specifics pay off.',
      )}
      width={620}
      footer={
        statement ? (
          <>
            <button className="btn btn-ghost sm" onClick={onClose}>
              {tr('取消', 'Cancel')}
            </button>
            <button
              className="btn btn-primary sm"
              disabled={statement.trim().length < 10}
              onClick={() => {
                onDone(statement.trim());
                onClose();
              }}
            >
              {tr('用这段描述', 'Use this statement')}
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-ghost sm" onClick={onClose}>
              {tr('取消', 'Cancel')}
            </button>
            <button
              className="btn btn-primary sm"
              disabled={busy || !question || nothingChosen}
              title={nothingChosen ? tr('至少选一项或自己写一句', 'Pick at least one, or write your own') : undefined}
              onClick={submitAnswer}
            >
              {busy ? tr('思考中…', 'Thinking…') : tr('下一步', 'Next')}
            </button>
          </>
        )
      }
    >
      {/* 进度 */}
      <div className="row gap6" style={{ alignItems: 'center', margin: '2px 0 14px' }}>
        {Array.from({ length: total }, (_, i) => (
          <span
            key={i}
            style={{
              flex: 1,
              height: 3,
              borderRadius: 2,
              background: i < step ? 'var(--accent)' : 'var(--border)',
            }}
          />
        ))}
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginLeft: 4 }}>
          {statement ? tr('完成', 'done') : `${step}/${total}`}
        </span>
      </div>

      {statement ? (
        <div className="col gap8">
          <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5 }}>
            {tr(
              '按访谈内容写的英文描述——语料是英文论文摘要，中文描述会让匹配失准。可以直接改。',
              'Written in English: the corpus is English paper abstracts, and a Chinese statement skews the matching. Edit freely.',
            )}
          </div>
          <textarea
            className="textarea"
            rows={8}
            value={statement}
            onChange={(e) => setStatement(e.target.value)}
          />
        </div>
      ) : busy && !question ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('正在准备问题…', 'Preparing the question…')}
        </div>
      ) : question ? (
        <div className="col gap10">
          <div>
            <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
              {question.title}
            </div>
            <div style={{ fontSize: 13.5, fontWeight: 600, marginTop: 4, lineHeight: 1.5 }}>
              {question.question}
            </div>
          </div>

          {question.options.length > 0 && (
            <div className="col gap6">
              {question.options.map((opt) => {
                const on = picked.includes(opt);
                return (
                  <label
                    key={opt}
                    className="row gap8"
                    style={{
                      alignItems: 'flex-start',
                      padding: '9px 11px',
                      borderRadius: 8,
                      cursor: 'pointer',
                      border: `0.5px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                      background: on ? 'var(--accent-soft)' : 'transparent',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() => toggle(opt)}
                      style={{ marginTop: 2, flexShrink: 0 }}
                    />
                    <span style={{ fontSize: 12.5, lineHeight: 1.5 }}>{opt}</span>
                  </label>
                );
              })}
            </div>
          )}

          <div className="col gap4">
            <span className="row gap6" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
              <Icon name="pen" size={11} />
              {question.options.length > 0
                ? tr('其他（自己写，可与上面同时选）', 'Other — write your own, combines with the above')
                : tr('请自己写（这次没能给出候选）', 'Write your own — no suggestions this time')}
            </span>
            <textarea
              className="textarea"
              rows={2}
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              placeholder={tr('补充这个环节的具体内容…', 'Add specifics for this step…')}
            />
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
