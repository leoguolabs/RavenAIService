import type { ProjectRepoOption } from '@/api'

export function filterProjectRepoOptions(options: ProjectRepoOption[], query: string): ProjectRepoOption[] {
  const keyword = query.trim().toLowerCase()
  if (!keyword) return options

  return options.filter((option) =>
    option.project_name.toLowerCase().includes(keyword) || option.project_code.toLowerCase().includes(keyword),
  )
}
