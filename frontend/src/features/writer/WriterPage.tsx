import { UnderConstruction } from '../shared/UnderConstruction';

export function WriterPage() {
  return (
    <UnderConstruction
      eyebrow="Stage 04 · Paper Writer"
      icon="pen"
      title="论文撰写"
      titleEn="Paper Writer"
      sub="从实验产物到可编译投稿稿件的自动写作流水线"
      subEn="From experiment artifacts to a compilable, submission-ready draft"
      milestone="M5"
      features={[
        { zh: '分节草稿生成', desc: 'Abstract → Intro → Related → Method → Experiments → Conclusion 分节撰写，进度与字数跟踪。' },
        { zh: '引用真实性校验', desc: 'bib 全量对 Semantic Scholar 校验存在性，杜绝幻觉引用；引用论述与原文一致性检查。' },
        { zh: 'LaTeX 编译检查', desc: 'latexmk 编译、overfull/undefined refs/缺图检测，页数限制与会议模板适配。' },
        { zh: '匿名化与图表质检', desc: '双盲匿名检查；VLM 审查指标图（坐标轴/图例/误差棒）并回灌修图。' },
        { zh: '投稿闸门', desc: 'paper_submission 闸门：人工确认 venue/deadline 后才允许投稿动作。' },
      ]}
    />
  );
}
