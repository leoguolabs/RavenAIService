import { describe, expect, it } from 'vitest'
import { filterProjectRepoOptions } from './projectRepoSearch'

const projects = [
  { id: 1, project_name: '灵犀10-OM', project_code: 'LX10', default_branch: 'main', project_card: '' },
  { id: 2, project_name: '路由-Radas', project_code: 'ROUTER', default_branch: 'main', project_card: '' },
  { id: 3, project_name: '灵犀07A-OM', project_code: 'LX07A', default_branch: 'main', project_card: '' },
]

describe('filterProjectRepoOptions', () => {
  it('matches part of a project name and preserves project order', () => {
    expect(filterProjectRepoOptions(projects, '灵犀')).toEqual([projects[0], projects[2]])
    expect(filterProjectRepoOptions(projects, 'radas')).toEqual([projects[1]])
  })

  it('matches project codes case-insensitively and ignores surrounding spaces', () => {
    expect(filterProjectRepoOptions(projects, ' lx07 ')).toEqual([projects[2]])
  })

  it('shows all projects when the query is cleared and none for an unmatched query', () => {
    expect(filterProjectRepoOptions(projects, '  ')).toEqual(projects)
    expect(filterProjectRepoOptions(projects, 'missing')).toEqual([])
  })
})
