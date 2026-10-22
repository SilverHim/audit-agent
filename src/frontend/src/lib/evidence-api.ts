import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import {
  evidenceEditEvidenceRequestMutation,
  evidenceEvidenceUnavailableMutation,
  evidenceListEvidenceRequestsOptions,
  evidenceRegisterManagementRepresentationMutation,
  evidenceUploadDocumentsMutation,
} from '@/client/@tanstack/react-query.gen'
import type { EvidenceRequest } from '@/client/types.gen'
import { invalidateEngagement } from '@/lib/engagement'
import { requestStatus, requestStatusChanged, requestStatuses } from '@/lib/evidence-requests'

// 补充证据请求的接口 (后端需求 R369, 页面 R370): 列表, 修改请求, 标无法取得, 按请求上传文件, 登记管理层说明.
// 都是生成的客户端; 写操作后按项目整体失效 (复核队列与总览跟着刷新).

// 请求列表. 还有在等证据的请求时每 3 秒轮询 (与数据管理页一致); 状态由等证据变成已收到或无法取得时刷新本项目其他
// 查询: 原复核任务回到待处理, 要在复核队列与待决定里看到. poll 为假时不轮询 (判断详情里的重判链接只看一次)
export function useEvidenceRequests(eid: string, { poll = true }: { poll?: boolean } = {}) {
  const queryClient = useQueryClient()
  const previous = useRef<Map<string, string> | null>(null)
  const query = useQuery({
    ...evidenceListEvidenceRequestsOptions({ path: { eid } }),
    refetchInterval: (current) =>
      poll && current.state.data?.some((item) => requestStatus(item) === 'open') ? 3000 : false,
  })
  const data: EvidenceRequest[] | undefined = query.data
  useEffect(() => {
    if (!data) return
    const changed = requestStatusChanged(previous.current, data)
    previous.current = requestStatuses(data)
    if (changed) void invalidateEngagement(queryClient, eid)
  }, [data, queryClient, eid])
  return query
}

export function useEditEvidenceRequest(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    ...evidenceEditEvidenceRequestMutation(),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

export function useMarkEvidenceUnavailable(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    ...evidenceEvidenceUnavailableMutation(),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 按补充证据请求上传文件: 请求记文件并置已收到, 原复核任务回到待处理 (后端 link_uploaded_documents)
export function useUploadEvidenceDocuments(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    ...evidenceUploadDocumentsMutation(),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

export function useRegisterManagementRepresentation(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    ...evidenceRegisterManagementRepresentationMutation(),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}
