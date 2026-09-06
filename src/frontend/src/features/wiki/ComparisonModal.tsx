import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { EmptyState } from '../../components/ui/EmptyState';
import { api } from '../../lib/api';
import {
  COMPARISON_FIELD_META,
  comparisonColumnTitle,
  comparisonToCsv,
  type ComparisonCell,
} from '../../lib/comparison';
import { tr } from '../../lib/i18n';
import { saveBlob } from './shared';

/* ============================================================
   论文对比表弹窗（#669，设计报告 §10 ④层）：论文列表多选 ≥2 篇后
   打开。行 = skeleton/method 的九个抽取字段，列 = 所选论文，横向
   滚动。数据全部来自存量抽取产物（零 LLM，秒开）；没抽过的论文
   格子灰显「未抽取」——缺口摆出来，比悄悄留空诚实。
   ============================================================ */

function fieldLabel(field: string): string {
  const meta = COMPARISON_FIELD_META[field];
  // 后端 schema 演进先于前端标签表时兜底显示字段名，不至于空一格
  return meta ? tr(meta.zh, meta.en) : field;
}

function CellContent({ cell }: { cell: ComparisonCell }) {
  if (!cell.present || cell.value == null) {
    return (
      <span
        style={{ color: 'var(--text-3)', fontStyle: 'italic' }}
        title={tr('可在论文详情里运行抽取', 'Run extraction from the paper detail pane')}
      >
        {tr('未抽取', 'Not extracted')}
      </span>
    );
  }
  return <>{cell.value}</>;
}

export function ComparisonModal({
  libraryId,
  paperIds,
  open,
  onClose,
}: {
  libraryId: string;
  paperIds: string[];
  open: boolean;
  onClose: () => void;
}) {
  const { data, isLoading, isError } = useQuery({
    // paperIds 顺序就是列序，进 key：换一批或换顺序都算新表
    queryKey: ['library-comparison', libraryId, paperIds],
    queryFn: () => api.buildLibraryComparison(libraryId, paperIds),
    enabled: open && paperIds.length >= 2,
    retry: false,
  });

  const exportCsv = () => {
    if (!data) return;
    const csv = comparisonToCsv(data, {
      fieldLabel,
      fieldColumnTitle: tr('字段', 'Field'),
      absentText: tr('未抽取', 'Not extracted'),
    });
    // BOM 让 Excel 认出 UTF-8（中文表头/正文不乱码）；纯函数不掺编码关注点，在这加
    saveBlob(
      new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' }),
      'polaris-comparison.csv',
    );
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={1080}
      title={
        <>
          <Icon name="grid" size={15} />
          {tr('论文对比', 'Compare papers')}
        </>
      }
      sub={tr(
        '按结构化抽取字段并排对比所选论文；「未抽取」的论文可在论文详情里运行抽取后再来。',
        'Side-by-side view over structured extraction fields; run extraction from the paper detail pane for “not extracted” papers.',
      )}
      footer={
        <div className="row gap8" style={{ justifyContent: 'flex-end' }}>
          <button className="btn btn-soft sm" disabled={!data} onClick={exportCsv}>
            <Icon name="download" size={13} />
            {tr('导出 CSV', 'Export CSV')}
          </button>
          <button className="btn btn-ghost sm" onClick={onClose}>
            {tr('关闭', 'Close')}
          </button>
        </div>
      }
    >
      {isLoading ? (
        <div className="empty">{tr('生成对比表…', 'Building comparison…')}</div>
      ) : isError || !data ? (
        <EmptyState
          compact
          icon="x"
          title={tr('无法生成对比表', 'Failed to build the comparison')}
          desc={tr('后端不可用或所选论文已不在本库，稍后重试。', 'Backend unavailable or the papers left this library — try again later.')}
        />
      ) : (
        <div style={{ overflowX: 'auto', maxHeight: '62vh', overflowY: 'auto' }}>
          <table
            style={{
              borderCollapse: 'collapse',
              fontSize: 12.5,
              lineHeight: 1.6,
              // 每列给个最小宽度，列多时横向滚动而不是挤成竖条
              minWidth: 160 + data.papers.length * 240,
            }}
          >
            <thead>
              <tr>
                <th
                  style={{
                    position: 'sticky',
                    left: 0,
                    top: 0,
                    zIndex: 2,
                    background: 'var(--bg-1)',
                    textAlign: 'left',
                    padding: '8px 10px',
                    borderBottom: '1px solid var(--border)',
                    minWidth: 96,
                  }}
                >
                  {tr('字段', 'Field')}
                </th>
                {data.papers.map((paper) => (
                  <th
                    key={paper.paper_id}
                    style={{
                      position: 'sticky',
                      top: 0,
                      zIndex: 1,
                      background: 'var(--bg-1)',
                      textAlign: 'left',
                      padding: '8px 10px',
                      borderBottom: '1px solid var(--border)',
                      minWidth: 220,
                      maxWidth: 320,
                      fontWeight: 640,
                    }}
                    title={comparisonColumnTitle(paper)}
                  >
                    {paper.title}
                    {paper.year != null && (
                      <span className="mono" style={{ marginLeft: 6, fontSize: 10.5, color: 'var(--text-3)', fontWeight: 400 }}>
                        {paper.year}
                      </span>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.field}>
                  <td
                    style={{
                      position: 'sticky',
                      left: 0,
                      background: 'var(--bg-1)',
                      padding: '8px 10px',
                      borderBottom: '0.5px solid var(--border)',
                      fontWeight: 620,
                      whiteSpace: 'nowrap',
                      verticalAlign: 'top',
                    }}
                  >
                    {fieldLabel(row.field)}
                  </td>
                  {row.cells.map((cell, i) => (
                    <td
                      key={data.papers[i]?.paper_id ?? i}
                      style={{
                        padding: '8px 10px',
                        borderBottom: '0.5px solid var(--border)',
                        verticalAlign: 'top',
                        minWidth: 220,
                        maxWidth: 320,
                      }}
                    >
                      <CellContent cell={cell} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}
