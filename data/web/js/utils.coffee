@defer = (timeout, callback) -> setTimeout callback, timeout
@repeat = (timeout, callback) ->
    id = setInterval ( ->
        if callback() == false
            clearInterval(id)
    ), timeout

@section_set_disabled = (id, disabled) ->
    $section = $("##{id}")
    $section.find(':input').attr 'disabled', disabled
    $section.find('.ui-buttonset').buttonset 'option', 'disabled', disabled
    $section.find('.ui-button').button 'option', 'disabled', disabled
    $section.find('.ui-toggleswitch').toggleswitch 'option', 'disabled', disabled
    $section.find('select').selectmenu 'option', 'disabled', disabled
    $section.find('label,img,p,span').css opacity: if disabled then 0.3 else 1.0

@section_toggleswitch = (section, toggle) ->
    toggle.change ->
        section_set_disabled section, not $(@).is(':checked')
    .change()


class @Stagehand
    constructor: (@root) ->
        @jobs = {}
        @timer = null
        @max_interval = 10000
        @poll(1000)

    api: (url, data={}, type='GET') ->
        dfd = $.Deferred()
        $.ajax(url: @root + url, data: data, type: type.toUpperCase())
            .done (response) =>
                if not response.jobid?
                    # Not a webcoroutine
                    return dfd.resolve(response)
                @jobs[response.jobid] = dfd
                if response.pending
                    dfd.notify response.jobid
                if response.pending and @interval > response.interval
                    # Job is pending and suggested interval from server is
                    # less than what the current interval is, so restart
                    # the poll timer.
                    @poll(response.interval)
                else if @interval > 1000
                    # Even if the job isn't pending, we're not idle, so
                    # increase the poll frequency.
                    @poll(1000)
                @handle_response response

            .fail (xhr, status, error) =>
                dfd.reject message: "HTTP #{xhr.status}: #{xhr.statusText}", xhr: xhr
        return dfd.promise()


    handle_response: ({jobs, notifications}) ->
        for job in jobs
            if @jobs[job.id]
                if job.error
                    @jobs[job.id].reject job.error
                else
                    @jobs[job.id].resolve job.result
                delete @jobs[job.id]
        for n in notifications
            for key, value of n
                if typeof value == 'string'
                    # Replace instances of {{root}} in string-based values with
                    # root.  Poor-man's template variable for notifications.
                    value = value.replace(/{{root}}/g, @root)
                n['pnotify_' + key] = value
            $.pnotify n

    poll: (interval=@interval) ->
        if @timer
            if interval == @interval
                # Timer already running at this interval
                return
            clearInterval @timer

        @interval = if interval <= @max_interval then interval else @max_interval
        @timer = repeat @interval, =>
            # FIXME: need a way to handle timeouts of pending jobs.
            # If there are pending jobs, pass them as a query parameter
            data = if not $.isEmptyObject(@jobs) then {jobs: (jobid for jobid, dfd of @jobs).join(',')} else null
            $.ajax(url: @root + '/api/jobs', data: data, timeout: @interval)
                .done ({jobs, notifications}) =>
                    @handle_response {jobs, notifications}
                    # If we have no active jobs or notifications and we're below the max interval,
                    # then back off.
                    if $.isEmptyObject(@jobs) and notifications.length == 0 and @interval < @max_interval
                        @poll @interval * 2

                .fail (xhr, status, error) =>
                    # Some type of error occured (network failure?), so drop to
                    # the max interval straightaway.
                    @poll @max_interval

$ ->
    # Fix click-drag bug on buttonset: http://bugs.jqueryui.com/ticket/7665
    $('label.ui-button').click ->
        b = $(@).prev()
        if not b.is(':disabled')
            if b.is(':checked') and b.attr('type') != 'radio'
                b.removeAttr('checked')
            else
                b.attr('checked', 'checked')
            b.button('refresh')
            b.change()
            false
    .on 'selectstart', -> false

$.pnotify.defaults.pnotify_opacity = 0.9
$.fn.poshytip.defaults.className = 'tip-twitter'
$.fn.poshytip.defaults.fade = false
$.fn.poshytip.defaults.slide = false
