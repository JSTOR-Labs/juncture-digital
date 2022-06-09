<template>
  <div id="essay-component" ref="essay" v-html="html"></div>
</template>

<script>
module.exports = {  
  name: 'SimpleEssay',
  props: {
    markdown: { type: String, default: '' },
    path: String
  },
  data: () => ({
    html: ''
  }),
  computed: {},
  mounted() {
    console.log(`${this.$options.name}.mounted path=${this.path}`)
    document.getElementById('app').classList.add('simple-essay')
    this.addStylesheet('https://visual-essays.github.io/web-app/static/css/main.css')
    this.addStylesheet('https://visual-essays.github.io/web-app/static/css/default-theme.css')
    this.addStylesheet('https://visual-essays.github.io/web-app/static/css/default-layout.css')
  },
  methods: {
    addStylesheet(url) {
      let el = document.createElement('link')
      el.href = url
      el.rel='stylesheet'
      document.getElementsByTagName('head')[0].appendChild(el)
    },
    addMainJs() {
      let mainJs = document.querySelector('script[src$="/static/js/main.js"]')
      if (mainJs) mainJs.parentElement.removeChild(mainJs)
      const script = document.createElement('script')
      script.type = 'text/javascript'
      script.src = `${location.hostname === 'localhost' ? 'http://localhost:8080' : 'https://visual-essays.net'}/static/js/main.js`
      document.getElementsByTagName('body')[0].appendChild(script)
    }
  },
  watch: {
    markdown: {
      handler: function (markdown) {
        fetch(`https://api.visual-essays.net/html/?base=${this.path}`,{
          method:'POST',
          body: markdown
        })
        .then(resp => resp.text())
        .then(html => {
          let el = new DOMParser().parseFromString(html, 'text/html').children[0].children[1]
          this.html = el.innerHTML
          this.addMainJs()
        })
      },
      immediate: true
    },
  }
}
</script>

<style scoped>
</style>
