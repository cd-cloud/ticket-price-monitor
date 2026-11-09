const dashboardState = {
  data: {
    tasks: [],
    routes: [],
    provider_options: [],
    cabin_options: [],
    last_run: { running: false, saved: 0, errors: [] },
  },
  activeTaskView: "all",
  pollTimer: null,
  pollInFlight: false,
  taskRenderSignature: "",
  routeRenderSignature: "",
  optionsSignature: "",
  sessionHintSignature: "",
};

const workspaceState = {
  activeTab: localStorage.getItem("activeWorkspaceTab") || "routes",
};

const browserProfileState = {
  data: { providers: [] },
};

const tailState = {
  form: {
    safeBatchSize: 3,
    defaultProfiles: {},
    candidateProfileOverride: "",
    candidatesManual: false,
    savedCandidates: null,
    candidatesLoaded: false,
    touched: false,
    candidateNames: window.tailCandidateNames || {},
  },
  job: {
    activeId: "",
    activeTimer: null,
    items: [],
    selectedId: "",
    selected: null,
    pollFailures: 0,
  },
  results: {
    items: [],
    view: "current",
    request: 0,
    offset: 0,
    hasMore: false,
    loaded: false,
    disclosureState: new Map(),
  },
  connection: {
    unknown: false,
    submitting: false,
  },
};

window.tailCandidateNames = tailState.form.candidateNames;
