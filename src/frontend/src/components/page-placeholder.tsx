import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

type Props = {
  title: string
  description: string
  // 该页要用到的端点，见 开发接口文档.md 第 10 节
  endpoints: string[]
}

// 页面骨架：C 实现页面时替换掉
export function PagePlaceholder({ title, description, endpoints }: Props) {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="font-heading text-2xl font-semibold">{title}</h1>
        <p className="text-muted-foreground">{description}</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>
            Not implemented yet <Badge variant="secondary">scaffold</Badge>
          </CardTitle>
          <CardDescription>Data sources</CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="list-disc space-y-1 pl-5 font-mono text-sm">
            {endpoints.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  )
}
