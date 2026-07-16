import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { api } from '../../lib/api';

/* /join/:token — 邀请链接落地页：预览方向信息并加入。 */

export function JoinPage() {
  const { token = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setCurrentProjectId } = useProject();

  const { data: info, isLoading, isError } = useQuery({
    queryKey: ['invite-info', token],
    queryFn: () => api.inviteInfo(token),
    retry: false,
    enabled: !!token,
  });

  const acceptMutation = useMutation({
    mutationFn: () => api.acceptInvite(token),
    onSuccess: (project) => {
      toast(`已加入研究方向：${project.name}`, 'ok');
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      setCurrentProjectId(project.id);
      navigate(`/projects/${project.id}`);
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(msg === 'INVITE_INVALID' ? '邀请链接已失效' : `加入失败：${msg}`, 'error');
    },
  });

  return (
    <div className="page fadeup">
      <div className="card card-pad" style={{ maxWidth: 480, margin: '80px auto 0', textAlign: 'center', padding: '48px 36px' }}>
        <div
          style={{
            width: 52, height: 52, borderRadius: 14, margin: '0 auto 18px',
            background: 'var(--accent-soft)', color: 'var(--accent)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <Icon name="users" size={24} />
        </div>
        {isLoading ? (
          <div style={{ fontSize: 13, color: 'var(--text-2)' }}>正在验证邀请链接…</div>
        ) : isError || !info ? (
          <>
            <div style={{ fontSize: 16, fontWeight: 680, marginBottom: 8 }}>邀请链接无效</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 20 }}>链接不存在或已被撤销，请向邀请人索取新链接。</div>
            <button className="btn btn-ghost" onClick={() => navigate('/')}>返回总览</button>
          </>
        ) : info.already_member ? (
          <>
            <div style={{ fontSize: 16, fontWeight: 680, marginBottom: 8 }}>你已是该方向成员</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 20 }}>{info.project_name}</div>
            <button className="btn btn-primary" onClick={() => navigate(`/projects/${info.project_id}`)}>进入方向</button>
          </>
        ) : !info.valid ? (
          <>
            <div style={{ fontSize: 16, fontWeight: 680, marginBottom: 8 }}>邀请链接已失效</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 20 }}>链接已过期或使用次数用尽，请向邀请人索取新链接。</div>
            <button className="btn btn-ghost" onClick={() => navigate('/')}>返回总览</button>
          </>
        ) : (
          <>
            <div style={{ fontSize: 16, fontWeight: 680, marginBottom: 8 }}>邀请你加入研究方向</div>
            <div style={{ fontSize: 14, color: 'var(--text)', marginBottom: 4 }}>{info.project_name}</div>
            {info.inviter_name && (
              <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 20 }}>邀请人：{info.inviter_name}</div>
            )}
            <button
              className="btn btn-primary"
              disabled={acceptMutation.isPending}
              onClick={() => acceptMutation.mutate()}
            >
              {acceptMutation.isPending ? '加入中…' : '加入该方向'}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
